"""Integration tests for curator pipeline wiring into mining flow."""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest

from codeprobe.mining.curator import CuratedFile, CurationResult, MergeConfig
from codeprobe.mining.org_scale import generate_org_scale_task
from codeprobe.mining.org_scale_families import MIGRATION_INVENTORY
from codeprobe.mining.org_scale_scanner import FamilyScanResult, PatternHit
from codeprobe.mining.writer import write_task_dir
from codeprobe.models.task import Task, TaskMetadata, TaskVerification

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def scan_result(tmp_path: Path) -> FamilyScanResult:
    repo = tmp_path / "test-repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    return FamilyScanResult(
        family=MIGRATION_INVENTORY,
        hits=(
            PatternHit("src/a.py", 10, "@Deprecated", r"@[Dd]eprecated"),
            PatternHit("src/b.py", 20, "@deprecated", r"@[Dd]eprecated"),
            PatternHit("src/c.py", 30, "@Deprecated", r"@[Dd]eprecated"),
        ),
        repo_paths=(repo,),
        commit_sha="abc123",
        matched_files=frozenset({"src/a.py", "src/b.py", "src/c.py"}),
    )


@pytest.fixture()
def curation_result(scan_result: FamilyScanResult) -> CurationResult:
    return CurationResult(
        family=MIGRATION_INVENTORY,
        files=(
            CuratedFile(
                path="src/a.py",
                tier="required",
                sources=("grep", "pr_diff"),
                confidence=0.95,
            ),
            CuratedFile(
                path="src/b.py",
                tier="supplementary",
                sources=("grep",),
                confidence=0.7,
            ),
            CuratedFile(
                path="src/c.py",
                tier="required",
                sources=("grep", "sourcegraph"),
                confidence=0.85,
            ),
        ),
        repo_paths=scan_result.repo_paths,
        commit_shas={"test-repo": "abc123"},
        backends_used=("grep", "pr_diff", "sourcegraph"),
        merge_config=MergeConfig(),
        matched_files=frozenset({"src/a.py", "src/b.py", "src/c.py"}),
    )


# ---------------------------------------------------------------------------
# generate_org_scale_task with CurationResult
# ---------------------------------------------------------------------------


class TestGenerateWithCuration:
    def test_oracle_tiers_populated(
        self,
        scan_result: FamilyScanResult,
        curation_result: CurationResult,
    ) -> None:
        task = generate_org_scale_task(
            scan_result,
            no_llm=True,
            curation_result=curation_result,
        )
        assert task is not None
        assert task.verification.oracle_tiers == {
            "src/a.py": "required",
            "src/b.py": "supplementary",
            "src/c.py": "required",
        }

    def test_ground_truth_from_curation(
        self,
        scan_result: FamilyScanResult,
        curation_result: CurationResult,
    ) -> None:
        task = generate_org_scale_task(
            scan_result,
            no_llm=True,
            curation_result=curation_result,
        )
        assert task is not None
        assert set(task.verification.oracle_answer) == {
            "src/a.py",
            "src/b.py",
            "src/c.py",
        }

    def test_without_curation_no_oracle_tiers(
        self,
        scan_result: FamilyScanResult,
    ) -> None:
        task = generate_org_scale_task(scan_result, no_llm=True)
        assert task is not None
        assert task.verification.oracle_tiers == {}


# ---------------------------------------------------------------------------
# Writer: ground_truth.json with curation
# ---------------------------------------------------------------------------


class TestWriterCuration:
    def test_schema_version_2_with_curation(
        self,
        scan_result: FamilyScanResult,
        curation_result: CurationResult,
        tmp_path: Path,
    ) -> None:
        task = generate_org_scale_task(
            scan_result,
            no_llm=True,
            curation_result=curation_result,
        )
        assert task is not None
        task_dir = write_task_dir(
            task,
            tmp_path,
            scan_result.repo_paths[0],
            curation_backends=("grep", "pr_diff", "sourcegraph"),
        )
        gt = json.loads((task_dir / "ground_truth.json").read_text())
        assert gt["schema_version"] == 2
        assert gt["oracle_tiers"] == {
            "src/a.py": "required",
            "src/b.py": "supplementary",
            "src/c.py": "required",
        }
        assert "curation" in gt
        assert set(gt["curation"]["backends_used"]) == {
            "grep",
            "pr_diff",
            "sourcegraph",
        }

    def test_schema_version_1_without_curation(
        self,
        scan_result: FamilyScanResult,
        tmp_path: Path,
    ) -> None:
        task = generate_org_scale_task(scan_result, no_llm=True)
        assert task is not None
        task_dir = write_task_dir(task, tmp_path, scan_result.repo_paths[0])
        gt = json.loads((task_dir / "ground_truth.json").read_text())
        assert gt["schema_version"] == 1
        assert "oracle_tiers" not in gt
        assert "curation" not in gt

    def test_backward_compat_ground_truth_format(
        self,
        scan_result: FamilyScanResult,
        tmp_path: Path,
    ) -> None:
        """Without curation, ground_truth.json has same keys as before + schema_version."""
        task = generate_org_scale_task(scan_result, no_llm=True)
        assert task is not None
        task_dir = write_task_dir(task, tmp_path, scan_result.repo_paths[0])
        gt = json.loads((task_dir / "ground_truth.json").read_text())
        # Required keys
        assert "oracle_type" in gt
        assert "expected" in gt
        assert "commit" in gt
        assert "pattern_used" in gt


# ---------------------------------------------------------------------------
# CLI flag validation
# ---------------------------------------------------------------------------


class TestCLIValidation:
    def test_agent_no_llm_raises_error(self) -> None:
        from codeprobe.cli.mine_cmd import run_mine

        with pytest.raises(click.UsageError, match="AgentSearchBackend requires"):
            run_mine(
                path="/nonexistent",
                no_llm=True,
                backends=("agent",),
                curate=True,
            )

    def test_curate_without_agent_and_no_llm_succeeds_validation(self) -> None:
        """--curate --no-llm --backends grep should not raise on flag validation."""
        # We only test that validation passes — actual mining would fail
        # because the path doesn't exist, so we catch SystemExit.
        from codeprobe.cli.mine_cmd import run_mine

        with pytest.raises(SystemExit):
            run_mine(
                path="/nonexistent",
                no_llm=True,
                backends=("grep",),
                curate=True,
            )


# ---------------------------------------------------------------------------
# CurationResult.from_scan_result bridge
# ---------------------------------------------------------------------------


class TestFromScanResultBridge:
    def test_round_trip_preserves_files(
        self,
        scan_result: FamilyScanResult,
    ) -> None:
        cr = CurationResult.from_scan_result(scan_result)
        assert cr.matched_files == scan_result.matched_files
        assert all(cf.tier == "required" for cf in cr.files)
        assert all(cf.sources == ("grep",) for cf in cr.files)
