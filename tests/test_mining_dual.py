"""Tests for Phase 2 dual-verification mining: oracle ground truth + discrimination."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from codeprobe.mining.extractor import (
    _build_oracle_ground_truth,
    _oracle_discrimination_passed,
)

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
