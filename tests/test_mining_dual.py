"""Tests for Phase 2 dual-verification mining: oracle ground truth + discrimination."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from codeprobe.cli import main
from codeprobe.mining.extractor import (
    _build_oracle_ground_truth,
    _oracle_discrimination_passed,
)
from codeprobe.models.task import Task, TaskMetadata, TaskVerification

# ---------------------------------------------------------------------------
# _build_oracle_ground_truth
# ---------------------------------------------------------------------------


class TestBuildOracleGroundTruth:
    """R16: Oracle ground truth generation from PR diff data."""

    def test_mixed_source_and_test_files(self, tmp_path: Path) -> None:
        """3 source + 2 test files → oracle with 3-file answer, test files excluded."""
        changed_files = [
            "src/auth/login.py",
            "src/auth/session.py",
            "src/core/config.py",
            "tests/test_login.py",
            "tests/test_session.py",
        ]
        # Mock symbol extraction to return 2 symbols
        with patch(
            "codeprobe.mining.extractor._extract_modified_symbols_from_diff",
            return_value=["authenticate", "refresh_token"],
        ):
            oracle = _build_oracle_ground_truth(
                merge_sha="abc12345",
                repo_path=tmp_path,
                changed_files=changed_files,
            )

        assert oracle is not None
        assert oracle["answer_type"] == "file_list"
        # Must use "answer" field, NOT "expected"
        assert "answer" in oracle
        assert "expected" not in oracle
        assert sorted(oracle["answer"]) == [
            "src/auth/login.py",
            "src/auth/session.py",
            "src/core/config.py",
        ]
        assert oracle["oracle_metadata"]["modified_symbols"] == [
            "authenticate",
            "refresh_token",
        ]

    def test_all_test_files_returns_none(self, tmp_path: Path) -> None:
        """All changed files are test files → returns None (empty oracle)."""
        changed_files = [
            "tests/test_login.py",
            "tests/test_session.py",
            "test/spec_helpers.py",
        ]
        with patch(
            "codeprobe.mining.extractor._extract_modified_symbols_from_diff",
            return_value=[],
        ):
            oracle = _build_oracle_ground_truth(
                merge_sha="abc12345",
                repo_path=tmp_path,
                changed_files=changed_files,
            )

        assert oracle is None

    def test_spec_files_excluded(self, tmp_path: Path) -> None:
        """Files with 'spec' in path are treated as test files and excluded."""
        changed_files = [
            "src/core/handler.go",
            "src/core/handler_test.go",
            "spec/handler_spec.rb",
        ]
        with patch(
            "codeprobe.mining.extractor._extract_modified_symbols_from_diff",
            return_value=["HandleRequest"],
        ):
            oracle = _build_oracle_ground_truth(
                merge_sha="abc12345",
                repo_path=tmp_path,
                changed_files=changed_files,
            )

        assert oracle is not None
        assert oracle["answer"] == ["src/core/handler.go"]

    def test_schema_version_is_1(self, tmp_path: Path) -> None:
        """Oracle ground truth uses schema_version 1."""
        changed_files = ["src/foo.py", "tests/test_foo.py"]
        with patch(
            "codeprobe.mining.extractor._extract_modified_symbols_from_diff",
            return_value=["do_thing"],
        ):
            oracle = _build_oracle_ground_truth(
                merge_sha="abc12345",
                repo_path=tmp_path,
                changed_files=changed_files,
            )

        assert oracle is not None
        assert oracle["schema_version"] == 1


# ---------------------------------------------------------------------------
# _oracle_discrimination_passed
# ---------------------------------------------------------------------------


class TestOracleDiscrimination:
    """R18: Discrimination gate for trivial oracles."""

    def test_files_spread_across_dirs_passes(self) -> None:
        """Files spread across multiple directories → passes with high confidence."""
        oracle = {
            "answer_type": "file_list",
            "answer": [
                "src/auth/login.py",
                "src/core/config.py",
                "lib/utils/helpers.py",
                "pkg/cache/redis.py",
            ],
        }
        passed, confidence = _oracle_discrimination_passed(oracle)
        assert passed is True
        assert confidence == "high"

    def test_most_files_in_one_dir_low_confidence(self) -> None:
        """>80% of files in one directory → passes but with low confidence."""
        oracle = {
            "answer_type": "file_list",
            "answer": [
                "src/auth/login.py",
                "src/auth/session.py",
                "src/auth/token.py",
                "src/auth/middleware.py",
                "src/core/config.py",
            ],
        }
        passed, confidence = _oracle_discrimination_passed(oracle)
        assert passed is True
        assert confidence == "low"

    def test_all_files_in_one_dir_low_confidence(self) -> None:
        """All files in same directory → low confidence."""
        oracle = {
            "answer_type": "file_list",
            "answer": [
                "src/auth/login.py",
                "src/auth/session.py",
                "src/auth/token.py",
            ],
        }
        passed, confidence = _oracle_discrimination_passed(oracle)
        assert passed is True
        assert confidence == "low"

    def test_single_file_oracle_low_confidence(self) -> None:
        """Single-file oracle → low confidence (trivially discoverable)."""
        oracle = {
            "answer_type": "file_list",
            "answer": ["src/auth/login.py"],
        }
        passed, confidence = _oracle_discrimination_passed(oracle)
        assert passed is True
        assert confidence == "low"

    def test_empty_answer_fails(self) -> None:
        """Empty answer list → fails discrimination."""
        oracle = {
            "answer_type": "file_list",
            "answer": [],
        }
        passed, confidence = _oracle_discrimination_passed(oracle)
        assert passed is False
        assert confidence == "low"

    def test_two_dirs_borderline(self) -> None:
        """Files in exactly 2 dirs, >80% in one → low confidence."""
        oracle = {
            "answer_type": "file_list",
            "answer": [
                "src/auth/a.py",
                "src/auth/b.py",
                "src/auth/c.py",
                "src/auth/d.py",
                "src/core/e.py",
            ],
        }
        passed, confidence = _oracle_discrimination_passed(oracle)
        assert passed is True
        assert confidence == "low"

    def test_evenly_split_high_confidence(self) -> None:
        """Files evenly split across dirs → high confidence."""
        oracle = {
            "answer_type": "file_list",
            "answer": [
                "src/auth/a.py",
                "src/auth/b.py",
                "src/core/c.py",
                "src/core/d.py",
                "lib/utils/e.py",
            ],
        }
        passed, confidence = _oracle_discrimination_passed(oracle)
        assert passed is True
        assert confidence == "high"


# ---------------------------------------------------------------------------
# Integration: --dual-verify flag wires oracle into writer output
# ---------------------------------------------------------------------------


class TestDualVerifyIntegration:
    """R17: --dual-verify flag produces dual verification tasks."""

    def test_dual_verify_produces_ground_truth(self, tmp_path: Path) -> None:
        """mine with dual_verify=True populates ground_truth.json with real oracle."""
        from codeprobe.mining.writer import write_task_dir
        from codeprobe.models.task import Task, TaskMetadata, TaskVerification

        metadata = TaskMetadata(
            name="merge-abc12345",
            difficulty="medium",
            description="Fix auth flow",
            language="python",
            category="comprehension",
            ground_truth_commit="abc1234567890",
        )
        verification = TaskVerification(
            type="test_script",
            command="pytest tests/test_auth.py",
            verification_mode="dual",
            reward_type="binary",
        )
        task = Task(
            id="abc12345",
            repo="myrepo",
            metadata=metadata,
            verification=verification,
        )

        task_dir = write_task_dir(task, tmp_path, tmp_path / "myrepo")

        # Check ground_truth.json exists in tests/
        gt_path = task_dir / "tests" / "ground_truth.json"
        assert gt_path.exists()
        gt = json.loads(gt_path.read_text())
        assert gt["schema_version"] == 1
        assert gt["answer_type"] == "file_list"

        # Check metadata.json has verification_mode: dual
        meta_path = task_dir / "metadata.json"
        meta = json.loads(meta_path.read_text())
        assert meta["verification"]["verification_mode"] == "dual"

        # Check test.sh exists
        test_sh = task_dir / "tests" / "test.sh"
        assert test_sh.exists()

    def test_dual_verify_with_populated_oracle(self, tmp_path: Path) -> None:
        """When oracle data is provided via ground_truth field, it's written."""
        from codeprobe.mining.writer import write_task_dir
        from codeprobe.models.task import Task, TaskMetadata, TaskVerification

        metadata = TaskMetadata(
            name="merge-def67890",
            difficulty="medium",
            description="Refactor config",
            language="python",
            category="comprehension",
            ground_truth_commit="def6789012345",
        )
        # Verification with oracle data populated
        verification = TaskVerification(
            type="test_script",
            command="pytest tests/test_config.py",
            verification_mode="dual",
            reward_type="binary",
            oracle_type="file_list",
            oracle_answer=("src/config.py", "src/settings.py"),
        )
        task = Task(
            id="def67890",
            repo="myrepo",
            metadata=metadata,
            verification=verification,
        )

        task_dir = write_task_dir(task, tmp_path, tmp_path / "myrepo")

        gt_path = task_dir / "tests" / "ground_truth.json"
        gt = json.loads(gt_path.read_text())
        assert gt["answer_type"] == "file_list"
        assert sorted(gt["answer"]) == ["src/config.py", "src/settings.py"]


# ---------------------------------------------------------------------------
# _apply_dual_verification: comprehension is NOT a PR-diff-oracle category
# ---------------------------------------------------------------------------


class TestComprehensionNotDualEligible:
    """codeprobe-lqct: comprehension tasks must never reach the PR-diff oracle.

    ``_apply_dual_verification`` builds oracles from a merge commit's changed
    files. Comprehension tasks are statically generated (no PR, no diff) and
    already carry their dual shape from ``ComprehensionGenerator.generate(
    dual=True)`` — ``verification_mode="dual"``, ``scoring_policy="min"``, and
    a static-analysis ``oracle_answer``. Routing them through the PR-diff
    constructor would overwrite that answer with a changed-file list.

    This locks the contract behaviourally: repointing the stale
    ``"comprehension"`` entry to the category the generator actually emits
    (``"architecture_comprehension"``) breaks these tests.
    """

    @staticmethod
    def _comprehension_task() -> Task:
        return Task(
            id="comprehension-import_chain-000-deadbeef",
            repo="myrepo",
            metadata=TaskMetadata(
                name="import_chain: pkg.a",
                difficulty="hard",
                description="Which modules does `pkg.a` import?",
                language="python",
                category="architecture_comprehension",
                task_type="architecture_comprehension",
                # Deliberately populated even though the real generator leaves
                # it empty: this is the maximal-trigger case, so the lock holds
                # even if comprehension tasks later gain a commit pin.
                ground_truth_commit="abc1234567890",
            ),
            verification=TaskVerification(
                type="artifact_eval",
                command="python3 -m codeprobe.core.scoring --artifact .",
                verification_mode="dual",
                ground_truth_path="tests/ground_truth.json",
                answer_schema="module_list",
                reward_type="artifact",
                scoring_policy="min",
                oracle_type="module_list",
                oracle_answer=("pkg.b", "pkg.c"),
            ),
        )

    def test_comprehension_task_passes_through_unchanged(self) -> None:
        """Generation-time dual shape survives _apply_dual_verification intact."""
        from codeprobe.cli.mine_cmd import _apply_dual_verification
        from codeprobe.mining.extractor import MineResult

        task = self._comprehension_task()
        mine_result = MineResult(
            tasks=[task],
            pr_bodies={},
            changed_files_map={task.id: ["src/pkg/a.py", "src/pkg/b.py"]},
        )

        result = _apply_dual_verification([task], mine_result, Path("/nonexistent"))

        assert result == [task]
        # The static-analysis answer must not be replaced by the PR file list.
        assert result[0].verification.oracle_answer == ("pkg.b", "pkg.c")

    def test_pr_diff_oracle_constructor_never_runs_for_comprehension(self) -> None:
        """The oracle builder is not even called — the category guard rejects first."""
        from codeprobe.cli.mine_cmd import _apply_dual_verification
        from codeprobe.mining.extractor import MineResult

        task = self._comprehension_task()
        mine_result = MineResult(
            tasks=[task],
            pr_bodies={},
            changed_files_map={task.id: ["src/pkg/a.py"]},
        )

        with patch(
            "codeprobe.mining.extractor._build_oracle_ground_truth"
        ) as build_oracle:
            _apply_dual_verification([task], mine_result, Path("/nonexistent"))

        build_oracle.assert_not_called()


# ---------------------------------------------------------------------------
# codeprobe-b31f: --dual-verify must demonstrably rewrite real producer output
# ---------------------------------------------------------------------------

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@test.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@test.com",
    "PATH": "/usr/bin:/bin",
}


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        check=True,
        text=True,
        env={**_GIT_ENV, "HOME": str(repo.parent)},
    )
    return result.stdout.strip()


def _make_sdlc_repo(tmp_path: Path) -> tuple[Path, str, list[str]]:
    """Build a real git repo whose HEAD commit has the shape of a merged PR.

    Returns ``(repo_path, head_sha, changed_files)``. The PR commit touches two
    source files in different directories (so the R18 discrimination gate scores
    the oracle "high") plus one test file (which the oracle must filter out).
    """
    repo = tmp_path / "sdlc-repo"
    (repo / "src").mkdir(parents=True)
    (repo / "lib").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "auth.py").write_text("def login(user):\n    return None\n")
    (repo / "lib" / "config.py").write_text("TIMEOUT = 1\n")
    (repo / "tests" / "test_auth.py").write_text("def test_login():\n    assert True\n")

    _git(repo, "init")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")

    # The "PR" commit.
    (repo / "src" / "auth.py").write_text(
        "def login(user, token):\n    return token\n"
    )
    (repo / "lib" / "config.py").write_text("TIMEOUT = 30\n")
    (repo / "tests" / "test_auth.py").write_text(
        "def test_login():\n    assert login('u', 't') == 't'\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Fix auth token expiry\n\nTokens expired silently.")

    head = _git(repo, "rev-parse", "HEAD")
    changed = ["lib/config.py", "src/auth.py", "tests/test_auth.py"]
    return repo, head, changed


class TestSdlcDualVerifyProducerConsumer:
    """codeprobe-b31f: the eligibility signal must come from the producer.

    The original gate matched ``metadata.category`` against a hardcoded set of
    snake_case names (``{"org_scale", "cross_repo"}``) that no producer has ever
    emitted, so ``--dual-verify`` rewrote zero tasks on every path that accepted
    the flag. These tests exercise the real producer → consumer path: the task is
    built by ``extract_task_from_merge`` (not hand-constructed), so a producer
    that stops declaring eligibility fails here instead of shipping a silent
    no-op.
    """

    def test_dual_verify_rewrites_real_sdlc_task(self, tmp_path: Path) -> None:
        from codeprobe.cli.mine_cmd import _apply_dual_verification
        from codeprobe.mining.extractor import MineResult, extract_task_from_merge

        repo, head, changed = _make_sdlc_repo(tmp_path)

        with patch(
            "codeprobe.mining.curator_backends.score_tool_benefit",
            return_value=("", ""),
        ):
            produced = extract_task_from_merge(
                head, repo, merge_title="Fix auth token expiry"
            )

        assert produced is not None, "fixture must yield a mineable SDLC task"
        task, _pr_meta = produced

        # The producer declares dual eligibility; the consumer must not have to
        # guess it from a category name.
        assert task.metadata.dual_eligible is True
        assert task.verification.verification_mode == "test_script"
        assert task.verification.oracle_answer == ()

        mine_result = MineResult(
            tasks=[task],
            pr_bodies={},
            changed_files_map={task.id: changed},
        )
        result = _apply_dual_verification([task], mine_result, repo)

        assert len(result) == 1
        dual = result[0]
        assert dual.verification.verification_mode == "dual"
        assert dual.verification.oracle_type == "file_list"
        # Test files are filtered out of the oracle answer.
        assert set(dual.verification.oracle_answer) == {"src/auth.py", "lib/config.py"}

    def test_org_scale_producer_output_is_not_rewritten(self, tmp_path: Path) -> None:
        """Producers that build their own oracle must never opt in.

        Routing an org-scale task through the PR-diff constructor would overwrite
        its family-scan ground truth with a changed-file list.
        """
        from codeprobe.cli.mine_cmd import _apply_dual_verification
        from codeprobe.mining.extractor import MineResult
        from codeprobe.mining.org_scale import mine_org_scale_tasks
        from codeprobe.mining.org_scale_families import MIGRATION_INVENTORY

        repo = tmp_path / "org-repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "old.py").write_text("@deprecated\ndef old_func(): pass\n")
        (repo / "src" / "legacy.py").write_text(
            "import warnings\n"
            "warnings.warn('Deprecated', DeprecationWarning)\n"
            "def legacy(): pass\n"
        )
        (repo / "src" / "also_old.py").write_text("@Deprecated\nclass OldClass: pass\n")
        _git(repo, "init")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "init")

        mined = mine_org_scale_tasks(
            [repo], count=2, families=(MIGRATION_INVENTORY,), no_llm=True
        )
        assert mined.tasks, "fixture must yield org-scale tasks"

        mine_result = MineResult(
            tasks=list(mined.tasks),
            pr_bodies={},
            changed_files_map={
                t.id: ["src/old.py", "src/legacy.py"] for t in mined.tasks
            },
        )
        result = _apply_dual_verification(list(mined.tasks), mine_result, repo)

        assert result == list(mined.tasks)
        for task in result:
            assert task.metadata.dual_eligible is False
            # Native family-scan oracle survives intact.
            assert task.verification.oracle_answer


class TestDualVerifyFlagIsHonest:
    """--dual-verify must never be accepted and silently dropped."""

    def test_org_scale_rejects_dual_verify(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        result = CliRunner().invoke(
            main,
            [
                "mine",
                str(repo),
                "--org-scale",
                "--dual-verify",
                "--no-interactive",
            ],
        )

        assert result.exit_code != 0
        assert "--dual-verify" in result.output

    def test_cross_repo_rejects_dual_verify(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        result = CliRunner().invoke(
            main,
            [
                "mine",
                str(repo),
                "--cross-repo",
                "/some/other/repo",
                "--dual-verify",
                "--no-interactive",
            ],
        )

        assert result.exit_code != 0
        assert "--dual-verify" in result.output


# ---------------------------------------------------------------------------
# dual_eligible survives the disk round-trip (b31f review, MEDIUM)
# ---------------------------------------------------------------------------
class TestDualEligibleRoundTrip:
    """``dual_eligible`` is persisted by write_task_dir; the loader must read it.

    Regression guard for the b31f review finding: the flag was serialized via
    ``dataclasses.asdict`` but ``_build_task`` rebuilt ``TaskMetadata`` from an
    explicit key allowlist that omitted it, so every reloaded task silently came
    back ``dual_eligible=False``. That is the same silent-divergence class the
    bead exists to kill, just moved to the loader.
    """

    def _write_metadata(self, tmp_path: Path, *, dual_eligible: bool) -> Path:
        task_dir = tmp_path / "task-001"
        task_dir.mkdir()
        (task_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "id": "task-001",
                    "repo": "acme/widgets",
                    "metadata": {
                        "name": "task-001",
                        "category": "sdlc",
                        "dual_eligible": dual_eligible,
                    },
                    "verification": {
                        "type": "test_script",
                        "command": "bash tests/test.sh",
                    },
                }
            )
        )
        return task_dir / "metadata.json"

    def test_true_survives_reload(self, tmp_path: Path) -> None:
        from codeprobe.loaders import load_task

        task = load_task(self._write_metadata(tmp_path, dual_eligible=True))
        assert task.metadata.dual_eligible is True

    def test_false_survives_reload(self, tmp_path: Path) -> None:
        from codeprobe.loaders import load_task

        task = load_task(self._write_metadata(tmp_path, dual_eligible=False))
        assert task.metadata.dual_eligible is False

    def test_absent_key_defaults_false(self, tmp_path: Path) -> None:
        """Task dirs mined before b31f have no such key — must not crash."""
        from codeprobe.loaders import load_task

        task_dir = tmp_path / "legacy"
        task_dir.mkdir()
        (task_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "id": "legacy",
                    "repo": "acme/widgets",
                    "metadata": {"name": "legacy", "category": "sdlc"},
                    "verification": {
                        "type": "test_script",
                        "command": "bash tests/test.sh",
                    },
                }
            )
        )
        assert load_task(task_dir / "metadata.json").metadata.dual_eligible is False
