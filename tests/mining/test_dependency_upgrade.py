"""Tests for the dependency_upgrade task generator (R3-new)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from codeprobe.mining.dependency_upgrade import (
    DEPENDENCY_MANIFESTS,
    PRCandidate,
    classify_with_model,
    generate_tasks,
    is_dependency_upgrade_candidate,
)
from codeprobe.mining.task_types import TASK_TYPE_REGISTRY, task_type_names
from codeprobe.mining.writer import write_task_dir
from codeprobe.models.task import TASK_TYPES

# ---------------------------------------------------------------------------
# Registry acceptance criteria (AC #1)
# ---------------------------------------------------------------------------


class TestRegistryRegistration:
    """The task-type registry must contain ``dependency_upgrade``."""

    def test_task_types_frozenset_contains(self) -> None:
        assert "dependency_upgrade" in TASK_TYPES

    def test_registry_contains_entry(self) -> None:
        assert "dependency_upgrade" in TASK_TYPE_REGISTRY

    def test_registry_entry_has_sdlc_dispatch_key(self) -> None:
        entry = TASK_TYPE_REGISTRY["dependency_upgrade"]
        assert entry.dispatch_key == "sdlc"

    def test_listed_in_task_type_names(self) -> None:
        assert "dependency_upgrade" in task_type_names()


# ---------------------------------------------------------------------------
# Structural filter
# ---------------------------------------------------------------------------


class TestStructuralFilter:
    def test_accept_package_json_plus_lockfile(self) -> None:
        pr = PRCandidate(
            sha="abc123",
            title="Bump lodash to 4.17.21",
            body="",
            changed_files=("package.json", "pnpm-lock.yaml"),
        )
        assert is_dependency_upgrade_candidate(pr)

    def test_accept_monorepo_subdir_manifest(self) -> None:
        pr = PRCandidate(
            sha="abc123",
            title="Bump axios to 1.6.0",
            body="",
            changed_files=("packages/web/package.json",),
        )
        assert is_dependency_upgrade_candidate(pr)

    def test_accept_title_with_bump_to_idiom_no_semver(self) -> None:
        pr = PRCandidate(
            sha="abc123",
            title="Bump transitive deps to latest",
            body="",
            changed_files=("package.json",),
        )
        assert is_dependency_upgrade_candidate(pr)

    def test_reject_non_manifest_file(self) -> None:
        pr = PRCandidate(
            sha="abc123",
            title="Bump lodash to 4.17.21",
            body="",
            changed_files=("package.json", "src/index.js"),
        )
        assert not is_dependency_upgrade_candidate(pr)

    def test_reject_empty_diff(self) -> None:
        pr = PRCandidate(
            sha="abc123",
            title="Bump lodash to 4.17.21",
            body="",
            changed_files=(),
        )
        assert not is_dependency_upgrade_candidate(pr)

    def test_reject_title_without_version_token(self) -> None:
        pr = PRCandidate(
            sha="abc123",
            title="Refactor build config",
            body="",
            changed_files=("package.json",),
        )
        assert not is_dependency_upgrade_candidate(pr)

    def test_dependency_manifests_set_contents(self) -> None:
        # Sanity: the core set we care about is present.
        for name in ("package.json", "pnpm-lock.yaml", "go.mod",
                     "Cargo.toml", "pyproject.toml", "Gemfile"):
            assert name in DEPENDENCY_MANIFESTS


# ---------------------------------------------------------------------------
# Model classification (AC #3)
# ---------------------------------------------------------------------------


def _mock_llm_response(decision: str, rationale: str) -> MagicMock:
    """Build a MagicMock shaped like an LLMResponse."""
    resp = MagicMock()
    resp.text = json.dumps({"decision": decision, "rationale": rationale})
    return resp


class TestModelClassifier:
    """classify_with_model must route through call_claude."""

    def test_accept_decision(self) -> None:
        pr = PRCandidate(
            sha="a" * 40,
            title="Bump lodash to 4.17.21",
            body="security fix",
            changed_files=("package.json", "pnpm-lock.yaml"),
        )
        with patch(
            "codeprobe.mining.dependency_upgrade.llm_available",
            return_value=True,
        ), patch(
            "codeprobe.mining.dependency_upgrade.call_claude",
            return_value=_mock_llm_response("accept", "dep bump"),
        ) as mock_call:
            decision, rationale = classify_with_model(pr)

        mock_call.assert_called_once()
        assert decision == "accept"
        assert "dep bump" in rationale

    def test_reject_on_llm_unavailable(self) -> None:
        pr = PRCandidate(
            sha="a" * 40,
            title="Bump lodash to 4.17.21",
            body="",
            changed_files=("package.json",),
        )
        with patch(
            "codeprobe.mining.dependency_upgrade.llm_available",
            return_value=False,
        ), patch(
            "codeprobe.mining.dependency_upgrade.call_claude",
        ) as mock_call:
            decision, rationale = classify_with_model(pr)
        mock_call.assert_not_called()
        assert decision == "reject"
        assert "unavailable" in rationale

    def test_reject_on_malformed_response(self) -> None:
        pr = PRCandidate(
            sha="a" * 40,
            title="Bump lodash to 4.17.21",
            body="",
            changed_files=("package.json",),
        )
        bad_resp = MagicMock()
        bad_resp.text = "not json at all"
        with patch(
            "codeprobe.mining.dependency_upgrade.llm_available",
            return_value=True,
        ), patch(
            "codeprobe.mining.dependency_upgrade.call_claude",
            return_value=bad_resp,
        ):
            decision, _rationale = classify_with_model(pr)
        assert decision == "reject"

    def test_reject_on_invalid_decision_value(self) -> None:
        pr = PRCandidate(
            sha="a" * 40,
            title="Bump lodash to 4.17.21",
            body="",
            changed_files=("package.json",),
        )
        resp = MagicMock()
        resp.text = json.dumps({"decision": "maybe", "rationale": "x"})
        with patch(
            "codeprobe.mining.dependency_upgrade.llm_available",
            return_value=True,
        ), patch(
            "codeprobe.mining.dependency_upgrade.call_claude",
            return_value=resp,
        ):
            decision, _rationale = classify_with_model(pr)
        assert decision == "reject"


# ---------------------------------------------------------------------------
# generate_tasks end-to-end (AC #2, #3, #4)
# ---------------------------------------------------------------------------


class TestGenerateTasks:
    """generate_tasks returns Tasks and invokes the model classifier."""

    def test_generates_task_for_pnpm_bump(self, tmp_path: Path) -> None:
        repo = tmp_path / "myrepo"
        repo.mkdir()
        pr = PRCandidate(
            sha="a" * 40,
            title="Bump lodash to 4.17.21",
            body="Security fix for prototype pollution.",
            changed_files=("package.json", "pnpm-lock.yaml"),
        )
        with patch(
            "codeprobe.mining.dependency_upgrade.llm_available",
            return_value=True,
        ), patch(
            "codeprobe.mining.dependency_upgrade.call_claude",
            return_value=_mock_llm_response("accept", "lodash bump"),
        ) as mock_call:
            tasks = generate_tasks(repo, [pr])

        # AC #3: model was called for classification.
        assert mock_call.call_count == 1

        # AC #2: at least one task emitted, correctly typed.
        assert len(tasks) == 1
        task = tasks[0]
        assert task.metadata.task_type == "dependency_upgrade"
        assert task.repo == "myrepo"
        assert task.metadata.ground_truth_commit == "a" * 40
        assert task.metadata.language == "javascript"
        assert task.metadata.enrichment_source == "llm"
        # Rationale from the model flows into tool_benefit_rationale.
        assert "lodash bump" in task.metadata.tool_benefit_rationale

    def test_skips_non_manifest_pr_without_calling_model(
        self, tmp_path: Path
    ) -> None:
        """Structural filter rejects before the model is asked."""
        repo = tmp_path / "myrepo"
        repo.mkdir()
        pr = PRCandidate(
            sha="b" * 40,
            title="Refactor auth flow",
            body="",
            changed_files=("src/auth.py", "tests/test_auth.py"),
        )
        with patch(
            "codeprobe.mining.dependency_upgrade.llm_available",
            return_value=True,
        ), patch(
            "codeprobe.mining.dependency_upgrade.call_claude",
        ) as mock_call:
            tasks = generate_tasks(repo, [pr])
        assert tasks == []
        mock_call.assert_not_called()

    def test_skips_when_model_rejects(self, tmp_path: Path) -> None:
        repo = tmp_path / "myrepo"
        repo.mkdir()
        pr = PRCandidate(
            sha="c" * 40,
            title="Bump lodash to 4.17.21",
            body="",
            changed_files=("package.json",),
        )
        with patch(
            "codeprobe.mining.dependency_upgrade.llm_available",
            return_value=True,
        ), patch(
            "codeprobe.mining.dependency_upgrade.call_claude",
            return_value=_mock_llm_response("reject", "looks like license change"),
        ):
            tasks = generate_tasks(repo, [pr])
        assert tasks == []

    def test_no_llm_mode_accepts_without_model(self, tmp_path: Path) -> None:
        repo = tmp_path / "myrepo"
        repo.mkdir()
        pr = PRCandidate(
            sha="d" * 40,
            title="Bump axios to 1.6.0",
            body="",
            changed_files=("package.json",),
        )
        with patch(
            "codeprobe.mining.dependency_upgrade.call_claude",
        ) as mock_call:
            tasks = generate_tasks(repo, [pr], no_llm=True)
        assert len(tasks) == 1
        mock_call.assert_not_called()
        assert tasks[0].metadata.task_type == "dependency_upgrade"


# ---------------------------------------------------------------------------
# End-to-end writer output (AC #4)
# ---------------------------------------------------------------------------


class TestWriterIntegration:
    """A mined task must produce the full writer-output directory."""

    def test_writer_produces_full_task_dir(self, tmp_path: Path) -> None:
        repo = tmp_path / "myrepo"
        repo.mkdir()

        pr = PRCandidate(
            sha="e" * 40,
            title="Bump lodash to 4.17.21",
            body="Security fix for prototype pollution.",
            changed_files=("package.json", "pnpm-lock.yaml"),
        )
        with patch(
            "codeprobe.mining.dependency_upgrade.llm_available",
            return_value=True,
        ), patch(
            "codeprobe.mining.dependency_upgrade.call_claude",
            return_value=_mock_llm_response("accept", "looks good"),
        ):
            tasks = generate_tasks(repo, [pr])

        assert len(tasks) == 1
        task = tasks[0]

        base_dir = tmp_path / "out" / "tasks"
        task_dir = write_task_dir(task, base_dir, repo)

        # instruction.md exists and has content
        instruction = (task_dir / "instruction.md").read_text(encoding="utf-8")
        assert "Bump lodash to 4.17.21" in instruction
        assert instruction.strip()

        # metadata.json exists, is valid JSON, and carries the new task_type
        metadata_raw = (task_dir / "metadata.json").read_text(encoding="utf-8")
        metadata = json.loads(metadata_raw)
        assert metadata["metadata"]["task_type"] == "dependency_upgrade"
        assert metadata["metadata"]["ground_truth_commit"] == "e" * 40

        # Oracle / test output files: the sdlc path produces tests/test.sh
        test_sh = task_dir / "tests" / "test.sh"
        assert test_sh.is_file()
        assert test_sh.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")
