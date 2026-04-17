"""Tests for SDLC ground truth extraction (bead codeprobe-br7.2)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from codeprobe.mining.writer import write_task_dir
from codeprobe.models.task import Task, TaskMetadata, TaskVerification

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sdlc_task(task_id: str = "abc12345") -> Task:
    """Construct a minimal standard SDLC Task for testing."""
    metadata = TaskMetadata(
        name=f"merge-{task_id}",
        difficulty="medium",
        description="Fix auth token refresh",
        language="python",
        category="sdlc",
        issue_title="Auth tokens expire silently",
        issue_body="Users report that auth tokens expire without warning.",
        ground_truth_commit="abc12345def67890abc12345def67890abc12345",
    )
    verification = TaskVerification(
        type="test_script",
        command="bash tests/test.sh",
        reward_type="binary",
    )
    return Task(
        id=task_id,
        repo="myrepo",
        metadata=metadata,
        verification=verification,
    )


def _make_oracle_task(task_id: str = "oracle01") -> Task:
    """Construct a minimal oracle Task for testing."""
    metadata = TaskMetadata(
        name=f"oracle-{task_id}",
        difficulty="medium",
        description="Find all files that handle auth",
        language="python",
        category="org_scale",
    )
    verification = TaskVerification(
        type="oracle",
        command="bash tests/test.sh",
        reward_type="binary",
        oracle_type="file_list",
        oracle_answer=("src/auth.py",),
    )
    return Task(
        id=task_id,
        repo="myrepo",
        metadata=metadata,
        verification=verification,
    )


def _mock_subprocess_diff_stat(stat_output: str):
    """Return a mock for subprocess.run that returns diff --stat output."""
    result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stat_output, stderr=""
    )
    return result


# ---------------------------------------------------------------------------
# Tests: _get_diff_stat
# ---------------------------------------------------------------------------


class TestGetDiffStat:
    def test_returns_stat_output(self) -> None:
        from codeprobe.mining.extractor import _get_diff_stat

        stat_text = (
            " src/auth.py | 12 +++---\n"
            " src/session.py |  8 ++++\n"
            " 2 files changed, 15 insertions(+), 5 deletions(-)\n"
        )
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=stat_text, stderr=""
        )
        with patch(
            "codeprobe.mining.extractor.subprocess.run", return_value=mock_result
        ):
            result = _get_diff_stat("abc1234567890abc", Path("/fake/repo"))

        assert "src/auth.py" in result
        assert "src/session.py" in result
        assert "2 files changed" in result

    def test_timeout_returns_empty(self) -> None:
        from codeprobe.mining.extractor import _get_diff_stat

        with patch(
            "codeprobe.mining.extractor.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=15),
        ):
            result = _get_diff_stat("abc1234567890abc", Path("/fake/repo"))

        assert result == ""

    def test_nonzero_returncode_returns_empty(self) -> None:
        from codeprobe.mining.extractor import _get_diff_stat

        mock_result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="error"
        )
        with patch(
            "codeprobe.mining.extractor.subprocess.run", return_value=mock_result
        ):
            result = _get_diff_stat("abc1234567890abc", Path("/fake/repo"))

        assert result == ""

    def test_truncates_long_output(self) -> None:
        from codeprobe.mining.extractor import _get_diff_stat

        # 60 lines of stat output — should be truncated to 50
        lines = [f" file{i:03d}.py | 1 +\n" for i in range(60)]
        stat_text = "".join(lines)
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=stat_text, stderr=""
        )
        with patch(
            "codeprobe.mining.extractor.subprocess.run", return_value=mock_result
        ):
            result = _get_diff_stat("abc1234567890abc", Path("/fake/repo"))

        assert len(result.splitlines()) <= 50


# ---------------------------------------------------------------------------
# Tests: _extract_modified_symbols_structured
# ---------------------------------------------------------------------------


class TestExtractModifiedSymbolsStructured:
    def test_python_symbols(self) -> None:
        from codeprobe.mining.extractor import _extract_modified_symbols_structured

        diff_output = (
            "diff --git a/src/auth.py b/src/auth.py\n"
            "--- a/src/auth.py\n"
            "+++ b/src/auth.py\n"
            "@@ -1,3 +1,5 @@\n"
            "+def authenticate(user, password):\n"
            "+    pass\n"
            "+class TokenManager:\n"
            "+    pass\n"
        )
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=diff_output, stderr=""
        )
        with patch(
            "codeprobe.mining.extractor.subprocess.run", return_value=mock_result
        ):
            symbols = _extract_modified_symbols_structured(
                "abc1234567890abc", Path("/fake/repo"), ["src/auth.py"]
            )

        assert len(symbols) >= 1
        files_in_result = {s["file"] for s in symbols}
        assert "src/auth.py" in files_in_result
        names_in_result = {s["symbol"] for s in symbols}
        assert "authenticate" in names_in_result

    def test_skips_private_symbols(self) -> None:
        from codeprobe.mining.extractor import _extract_modified_symbols_structured

        diff_output = (
            "diff --git a/src/util.py b/src/util.py\n"
            "+def _helper():\n"
            "+    pass\n"
            "+def public_fn():\n"
            "+    pass\n"
        )
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=diff_output, stderr=""
        )
        with patch(
            "codeprobe.mining.extractor.subprocess.run", return_value=mock_result
        ):
            symbols = _extract_modified_symbols_structured(
                "abc1234567890abc", Path("/fake/repo"), ["src/util.py"]
            )

        names = {s["symbol"] for s in symbols}
        assert "_helper" not in names
        assert "public_fn" in names

    def test_multi_file_associates_correctly(self) -> None:
        from codeprobe.mining.extractor import _extract_modified_symbols_structured

        diff_a = "+def foo():\n+    pass\n"
        diff_b = "+def bar():\n+    pass\n"

        def mock_run(cmd, **kwargs):
            file_path = cmd[-1]  # last arg is the file path
            if "a.py" in file_path:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout=diff_a, stderr=""
                )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=diff_b, stderr=""
            )

        with patch("codeprobe.mining.extractor.subprocess.run", side_effect=mock_run):
            symbols = _extract_modified_symbols_structured(
                "abc1234567890abc", Path("/fake/repo"), ["src/a.py", "src/b.py"]
            )

        by_file = {s["symbol"]: s["file"] for s in symbols}
        assert by_file.get("foo") == "src/a.py"
        assert by_file.get("bar") == "src/b.py"

    def test_deduplicates_by_file_symbol_pair(self) -> None:
        from codeprobe.mining.extractor import _extract_modified_symbols_structured

        # Same symbol appears on both + and - lines (modified, not added)
        diff_output = "-def authenticate(user):\n" "+def authenticate(user, token):\n"
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=diff_output, stderr=""
        )
        with patch(
            "codeprobe.mining.extractor.subprocess.run", return_value=mock_result
        ):
            symbols = _extract_modified_symbols_structured(
                "abc1234567890abc", Path("/fake/repo"), ["src/auth.py"]
            )

        auth_entries = [s for s in symbols if s["symbol"] == "authenticate"]
        assert len(auth_entries) == 1


# ---------------------------------------------------------------------------
# Tests: _build_sdlc_ground_truth
# ---------------------------------------------------------------------------


class TestBuildSdlcGroundTruth:
    @patch("codeprobe.mining.extractor._get_diff_stat", return_value="1 file changed")
    @patch(
        "codeprobe.mining.extractor._extract_modified_symbols_structured",
        return_value=[{"file": "src/auth.py", "symbol": "authenticate"}],
    )
    def test_splits_source_and_test_files(self, mock_symbols, mock_stat) -> None:
        from codeprobe.mining.extractor import _build_sdlc_ground_truth

        changed = ["src/auth.py", "src/session.py", "tests/test_auth.py"]
        result = _build_sdlc_ground_truth("abc1234567890abc", Path("/fake"), changed)

        assert set(result["source_files"]) == {"src/auth.py", "src/session.py"}
        assert result["test_files"] == ["tests/test_auth.py"]
        assert set(result["changed_files"]) == set(changed)

    @patch("codeprobe.mining.extractor._get_diff_stat", return_value="")
    @patch(
        "codeprobe.mining.extractor._extract_modified_symbols_structured",
        return_value=[],
    )
    def test_schema_version(self, mock_symbols, mock_stat) -> None:
        from codeprobe.mining.extractor import _build_sdlc_ground_truth

        result = _build_sdlc_ground_truth(
            "abc1234567890abc", Path("/fake"), ["src/x.py"]
        )
        assert result["schema_version"] == "sdlc-v1"
        assert result["populated_by"] == "mining-sdlc-ground-truth"

    @patch("codeprobe.mining.extractor._get_diff_stat", return_value="stat output")
    @patch(
        "codeprobe.mining.extractor._extract_modified_symbols_structured",
        return_value=[{"file": "src/auth.py", "symbol": "login"}],
    )
    def test_includes_symbols_and_diff_summary(self, mock_symbols, mock_stat) -> None:
        from codeprobe.mining.extractor import _build_sdlc_ground_truth

        result = _build_sdlc_ground_truth(
            "abc1234567890abc", Path("/fake"), ["src/auth.py"]
        )
        assert result["symbols"] == [{"file": "src/auth.py", "symbol": "login"}]
        assert result["diff_summary"] == "stat output"
        assert result["merge_sha"] == "abc1234567890abc"

    @patch("codeprobe.mining.extractor._get_diff_stat", return_value="")
    @patch(
        "codeprobe.mining.extractor._extract_modified_symbols_structured",
        return_value=[],
    )
    def test_all_test_files_still_returns_dict(self, mock_symbols, mock_stat) -> None:
        from codeprobe.mining.extractor import _build_sdlc_ground_truth

        result = _build_sdlc_ground_truth(
            "abc1234567890abc", Path("/fake"), ["tests/test_a.py"]
        )
        assert result["source_files"] == []
        assert result["test_files"] == ["tests/test_a.py"]
        assert result["schema_version"] == "sdlc-v1"


# ---------------------------------------------------------------------------
# Tests: _build_sdlc_ground_truth writable_paths (bead codeprobe-br7.5)
# ---------------------------------------------------------------------------


class TestSdlcGroundTruthWritablePaths:
    @patch("codeprobe.mining.extractor._get_diff_stat", return_value="")
    @patch(
        "codeprobe.mining.extractor._extract_modified_symbols_structured",
        return_value=[],
    )
    def test_emits_writable_paths_from_changed_files_parents(
        self, mock_symbols, mock_stat
    ) -> None:
        from codeprobe.mining.extractor import _build_sdlc_ground_truth

        changed = [
            "src/auth/login.py",
            "src/auth/session.py",
            "tests/test_auth.py",
        ]
        result = _build_sdlc_ground_truth(
            "abc1234567890abc", Path("/fake"), changed
        )
        # Includes test dir — CSB-style writable scope covers every directory
        # the PR touched, including tests/.
        assert result["writable_paths"] == ["src/auth", "tests"]

    @patch("codeprobe.mining.extractor._get_diff_stat", return_value="")
    @patch(
        "codeprobe.mining.extractor._extract_modified_symbols_structured",
        return_value=[],
    )
    def test_writable_paths_deduplicated_and_sorted(
        self, mock_symbols, mock_stat
    ) -> None:
        from codeprobe.mining.extractor import _build_sdlc_ground_truth

        changed = [
            "pkg/b/x.go",
            "pkg/a/y.go",
            "pkg/b/z.go",
            "pkg/a/y.go",
        ]
        result = _build_sdlc_ground_truth(
            "abc1234567890abc", Path("/fake"), changed
        )
        assert result["writable_paths"] == ["pkg/a", "pkg/b"]

    @patch("codeprobe.mining.extractor._get_diff_stat", return_value="")
    @patch(
        "codeprobe.mining.extractor._extract_modified_symbols_structured",
        return_value=[],
    )
    def test_root_level_changes_emit_filename_entry(
        self, mock_symbols, mock_stat
    ) -> None:
        """Root-level files (parent=='.') use the filename itself so the
        bash matcher `f == d or f.startswith(d + '/')` still discriminates."""
        from codeprobe.mining.extractor import _build_sdlc_ground_truth

        changed = ["setup.py", "README.md", "src/pkg/mod.py"]
        result = _build_sdlc_ground_truth(
            "abc1234567890abc", Path("/fake"), changed
        )
        assert result["writable_paths"] == [
            "README.md",
            "setup.py",
            "src/pkg",
        ]

    @patch("codeprobe.mining.extractor._get_diff_stat", return_value="")
    @patch(
        "codeprobe.mining.extractor._extract_modified_symbols_structured",
        return_value=[],
    )
    def test_writable_paths_empty_when_no_changed_files(
        self, mock_symbols, mock_stat
    ) -> None:
        from codeprobe.mining.extractor import _build_sdlc_ground_truth

        result = _build_sdlc_ground_truth(
            "abc1234567890abc", Path("/fake"), []
        )
        assert result["writable_paths"] == []

    @patch("codeprobe.mining.extractor._get_diff_stat", return_value="")
    @patch(
        "codeprobe.mining.extractor._extract_modified_symbols_structured",
        return_value=[],
    )
    def test_writable_paths_filters_unsafe_paths(
        self, mock_symbols, mock_stat
    ) -> None:
        """Traversal attempts are dropped before the parent-dir step."""
        from codeprobe.mining.extractor import _build_sdlc_ground_truth

        changed = ["../etc/passwd", "/absolute/path.py", "src/ok.py"]
        result = _build_sdlc_ground_truth(
            "abc1234567890abc", Path("/fake"), changed
        )
        assert result["writable_paths"] == ["src"]

    @patch("codeprobe.mining.extractor._get_diff_stat", return_value="")
    @patch(
        "codeprobe.mining.extractor._extract_modified_symbols_structured",
        return_value=[],
    )
    def test_writable_paths_uses_posix_separators(
        self, mock_symbols, mock_stat
    ) -> None:
        """Output must always use forward slashes regardless of mining host."""
        from codeprobe.mining.extractor import _build_sdlc_ground_truth

        changed = ["src/nested/deep/file.py"]
        result = _build_sdlc_ground_truth(
            "abc1234567890abc", Path("/fake"), changed
        )
        assert result["writable_paths"] == ["src/nested/deep"]
        assert all("\\" not in p for p in result["writable_paths"])


# ---------------------------------------------------------------------------
# Tests: write_task_dir ground_truth integration
# ---------------------------------------------------------------------------


class TestWriteTaskDirGroundTruth:
    def test_writes_ground_truth_json(self, tmp_path: Path) -> None:
        task = _make_sdlc_task()
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "myrepo"
        ground_truth = {
            "schema_version": "sdlc-v1",
            "changed_files": ["src/auth.py"],
            "source_files": ["src/auth.py"],
            "test_files": [],
            "symbols": [{"file": "src/auth.py", "symbol": "authenticate"}],
            "diff_summary": "1 file changed",
            "merge_sha": "abc12345def67890abc12345def67890abc12345",
            "populated_by": "mining-sdlc-ground-truth",
        }

        result_path = write_task_dir(
            task, base_dir, repo_path, ground_truth=ground_truth
        )

        gt_path = result_path / "tests" / "ground_truth.json"
        assert gt_path.is_file()
        data = json.loads(gt_path.read_text(encoding="utf-8"))
        assert data["schema_version"] == "sdlc-v1"
        assert data["symbols"] == [{"file": "src/auth.py", "symbol": "authenticate"}]

    def test_no_ground_truth_skips_file(self, tmp_path: Path) -> None:
        task = _make_sdlc_task()
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "myrepo"

        result_path = write_task_dir(task, base_dir, repo_path)

        gt_path = result_path / "tests" / "ground_truth.json"
        assert not gt_path.exists()

    def test_oracle_ignores_ground_truth_param(self, tmp_path: Path) -> None:
        """Oracle tasks use their own ground_truth.json — the sdlc param is ignored."""
        task = _make_oracle_task()
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "myrepo"
        sdlc_gt = {"schema_version": "sdlc-v1", "changed_files": ["should_not_appear"]}

        result_path = write_task_dir(task, base_dir, repo_path, ground_truth=sdlc_gt)

        # Oracle tasks return early before the SDLC ground_truth writing code.
        # The oracle writer creates its own ground_truth.json with oracle schema.
        # Verify the SDLC ground_truth was NOT written.
        gt_path = result_path / "tests" / "ground_truth.json"
        if gt_path.exists():
            data = json.loads(gt_path.read_text(encoding="utf-8"))
            # Must not be the SDLC schema we passed in
            assert data.get("schema_version") != "sdlc-v1"
        # Either way, the SDLC sentinel value must not appear anywhere
        task_dir_content = list(result_path.rglob("*.json"))
        for f in task_dir_content:
            assert "should_not_appear" not in f.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests: MineResult field
# ---------------------------------------------------------------------------


class TestMineResultGroundTruthMap:
    def test_default_empty(self) -> None:
        from codeprobe.mining.extractor import MineResult

        mr = MineResult(tasks=[], pr_bodies={}, changed_files_map={})
        assert mr.ground_truth_map == {}

    def test_accepts_ground_truth_map(self) -> None:
        from codeprobe.mining.extractor import MineResult

        gt = {"abc12345": {"schema_version": "sdlc-v1", "changed_files": ["a.py"]}}
        mr = MineResult(
            tasks=[],
            pr_bodies={},
            changed_files_map={},
            ground_truth_map=gt,
        )
        assert mr.ground_truth_map == gt
