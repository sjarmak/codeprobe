"""Tests for org-scale task mining — scanner, oracle-check, and integration."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from codeprobe.mining.org_scale import (
    OrgScaleMineResult,
    generate_org_scale_task,
    mine_org_scale_tasks,
)
from codeprobe.mining.org_scale_oracle import (
    extract_answer,
    normalize_path,
    oracle_check,
)
from codeprobe.mining.org_scale_scanner import (
    FamilyScanResult,
    PatternHit,
    scan_repo_for_family,
)
from codeprobe.mining.org_scale_families import (
    COMPLIANCE_AUDIT,
    CROSS_REPO_DEP_TRACE,
    FAMILIES,
    MIGRATION_INVENTORY,
    TaskFamily,
)
from codeprobe.mining.writer import write_task_dir
from codeprobe.models.task import (
    ORG_SCALE_CATEGORIES,
    Task,
    TaskMetadata,
    TaskVerification,
)

# ---------------------------------------------------------------------------
# Path normalization tests
# ---------------------------------------------------------------------------


class TestNormalizePath:
    def test_strip_dot_slash(self) -> None:
        assert normalize_path("./pkg/foo.go") == "pkg/foo.go"

    def test_strip_workspace_prefix(self) -> None:
        assert normalize_path("/workspace/pkg/foo.go") == "pkg/foo.go"

    def test_strip_tmp_prefix(self) -> None:
        assert normalize_path("/tmp/pkg/foo.go") == "pkg/foo.go"

    def test_strip_app_prefix(self) -> None:
        assert normalize_path("/app/src/main.py") == "src/main.py"

    def test_windows_backslash(self) -> None:
        assert normalize_path("pkg\\api\\server.go") == "pkg/api/server.go"

    def test_already_normalized(self) -> None:
        assert normalize_path("pkg/foo.go") == "pkg/foo.go"

    def test_trailing_whitespace(self) -> None:
        assert normalize_path("pkg/foo.go  \n") == "pkg/foo.go"

    def test_leading_slash(self) -> None:
        assert normalize_path("/pkg/foo.go") == "pkg/foo.go"

    def test_combined_prefixes(self) -> None:
        """Paths with multiple prefixes like /tmp/./pkg/foo.go are fully normalized."""
        assert normalize_path("/tmp/./pkg/foo.go") == "pkg/foo.go"
        assert normalize_path("/workspace/./src/main.py") == "src/main.py"
        assert normalize_path("/app/./config.yaml") == "config.yaml"

    def test_idempotent(self) -> None:
        """normalize(normalize(p)) == normalize(p)"""
        paths = [
            "./pkg/foo.go",
            "/workspace/src/main.py",
            "pkg\\bar.go",
            "/tmp/test.py",
            "/tmp/./pkg/foo.go",
            "/workspace/./src/main.py",
        ]
        for p in paths:
            once = normalize_path(p)
            twice = normalize_path(once)
            assert once == twice, f"Not idempotent: {p!r} → {once!r} → {twice!r}"


# ---------------------------------------------------------------------------
# extract_answer tests
# ---------------------------------------------------------------------------


class TestExtractAnswer:
    def test_reads_answer_txt(self, tmp_path: Path) -> None:
        (tmp_path / "answer.txt").write_text("pkg/a.go\npkg/b.go\n")
        result = extract_answer(tmp_path)
        assert result == ["pkg/a.go", "pkg/b.go"]

    def test_strips_blank_lines(self, tmp_path: Path) -> None:
        (tmp_path / "answer.txt").write_text("pkg/a.go\n\n\npkg/b.go\n\n")
        result = extract_answer(tmp_path)
        assert result == ["pkg/a.go", "pkg/b.go"]

    def test_strips_comments(self, tmp_path: Path) -> None:
        (tmp_path / "answer.txt").write_text("# header\npkg/a.go\n")
        result = extract_answer(tmp_path)
        assert result == ["pkg/a.go"]

    def test_normalizes_paths(self, tmp_path: Path) -> None:
        (tmp_path / "answer.txt").write_text("./pkg/a.go\n/workspace/pkg/b.go\n")
        result = extract_answer(tmp_path)
        assert result == ["pkg/a.go", "pkg/b.go"]

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        result = extract_answer(tmp_path)
        assert result == []

    def test_empty_file_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / "answer.txt").write_text("")
        result = extract_answer(tmp_path)
        assert result == []


# ---------------------------------------------------------------------------
# oracle_check tests (F1 scoring — premortem P0)
# ---------------------------------------------------------------------------


class TestOracleCheck:
    def _setup_task(
        self,
        tmp_path: Path,
        expected: list[str],
        agent_answer: list[str],
        commit: str = "abc123",
    ) -> Path:
        """Create a task dir with ground_truth.json and answer.txt."""
        task_dir = tmp_path / "task1"
        task_dir.mkdir()
        gt = {
            "oracle_type": "file_list",
            "expected": expected,
            "commit": commit,
        }
        (task_dir / "ground_truth.json").write_text(json.dumps(gt))
        (task_dir / "answer.txt").write_text("\n".join(agent_answer) + "\n")
        return task_dir

    def test_exact_match_scores_1(self, tmp_path: Path) -> None:
        task_dir = self._setup_task(
            tmp_path,
            expected=["pkg/a.go", "pkg/b.go"],
            agent_answer=["pkg/a.go", "pkg/b.go"],
        )
        result = oracle_check(task_dir)
        assert result["f1"] == 1.0
        assert result["precision"] == 1.0
        assert result["recall"] == 1.0
        assert result["score"] == 1.0  # default metric is f1

    def test_partial_match(self, tmp_path: Path) -> None:
        task_dir = self._setup_task(
            tmp_path,
            expected=["pkg/a.go", "pkg/b.go", "pkg/c.go"],
            agent_answer=["pkg/a.go", "pkg/b.go"],
        )
        result = oracle_check(task_dir)
        assert result["recall"] == pytest.approx(2 / 3, abs=0.01)
        assert result["precision"] == 1.0
        assert 0.0 < result["f1"] < 1.0

    def test_no_overlap_scores_0(self, tmp_path: Path) -> None:
        task_dir = self._setup_task(
            tmp_path,
            expected=["pkg/a.go"],
            agent_answer=["pkg/x.go"],
        )
        result = oracle_check(task_dir)
        assert result["f1"] == 0.0
        assert result["recall"] == 0.0
        assert result["precision"] == 0.0

    def test_empty_answer_scores_0(self, tmp_path: Path) -> None:
        task_dir = self._setup_task(tmp_path, expected=["pkg/a.go"], agent_answer=[])
        result = oracle_check(task_dir)
        assert result["score"] == 0.0
        assert "Empty agent answer" in result["error"]

    def test_missing_ground_truth(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "task_no_gt"
        task_dir.mkdir()
        (task_dir / "answer.txt").write_text("pkg/a.go\n")
        result = oracle_check(task_dir)
        assert result["score"] == 0.0
        assert "Missing" in result["error"]

    def test_path_normalization_in_comparison(self, tmp_path: Path) -> None:
        """./pkg/a.go and pkg/a.go are treated as equal."""
        task_dir = self._setup_task(
            tmp_path,
            expected=["pkg/a.go"],
            agent_answer=["./pkg/a.go"],
        )
        result = oracle_check(task_dir)
        assert result["f1"] == 1.0

    def test_duplicate_paths_after_normalization(self, tmp_path: Path) -> None:
        """Duplicates in answer don't inflate score (premortem P0: use sets)."""
        task_dir = self._setup_task(
            tmp_path,
            expected=["pkg/a.go", "pkg/b.go"],
            agent_answer=["./pkg/a.go", "pkg/a.go", "pkg/b.go", "./pkg/b.go"],
        )
        result = oracle_check(task_dir)
        # After dedup via frozenset, agent has 2 unique files matching 2 expected
        assert result["f1"] == 1.0
        assert result["answer_size"] == 2  # deduplicated

    def test_windows_paths_match(self, tmp_path: Path) -> None:
        task_dir = self._setup_task(
            tmp_path,
            expected=["pkg/api/server.go"],
            agent_answer=["pkg\\api\\server.go"],
        )
        result = oracle_check(task_dir)
        assert result["f1"] == 1.0

    def test_metric_recall(self, tmp_path: Path) -> None:
        task_dir = self._setup_task(
            tmp_path,
            expected=["pkg/a.go", "pkg/b.go"],
            agent_answer=["pkg/a.go", "pkg/b.go", "pkg/extra.go"],
        )
        result = oracle_check(task_dir, metric="recall")
        assert result["score"] == 1.0  # recall = 2/2 = 1.0
        assert result["precision"] < 1.0  # precision = 2/3

    def test_metric_jaccard(self, tmp_path: Path) -> None:
        task_dir = self._setup_task(
            tmp_path,
            expected=["pkg/a.go", "pkg/b.go"],
            agent_answer=["pkg/a.go"],
        )
        result = oracle_check(task_dir, metric="jaccard")
        assert result["score"] == pytest.approx(1 / 2, abs=0.01)  # 1/2

    def test_f1_invariant_bounds(self, tmp_path: Path) -> None:
        """F1 is always in [0, 1] — premortem P0: assert, don't clamp."""
        task_dir = self._setup_task(
            tmp_path,
            expected=["a.go", "b.go", "c.go"],
            agent_answer=["a.go", "d.go"],
        )
        result = oracle_check(task_dir)
        assert 0.0 <= result["f1"] <= 1.0
        assert 0.0 <= result["precision"] <= 1.0
        assert 0.0 <= result["recall"] <= 1.0


# ---------------------------------------------------------------------------
# Scanner tests
# ---------------------------------------------------------------------------


class TestScanner:
    def _make_repo(self, tmp_path: Path, files: dict[str, str]) -> Path:
        """Create a mock repo with git init and given files."""
        repo = tmp_path / "repo"
        repo.mkdir()
        for path, content in files.items():
            fp = repo / path
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_text(content)
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(
            ["git", "add", "."], cwd=str(repo), capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "init", "--allow-empty"],
            cwd=str(repo),
            capture_output=True,
            check=True,
            env={
                "GIT_AUTHOR_NAME": "test",
                "GIT_AUTHOR_EMAIL": "test@test.com",
                "GIT_COMMITTER_NAME": "test",
                "GIT_COMMITTER_EMAIL": "test@test.com",
                "PATH": "/usr/bin:/bin",
                "HOME": str(tmp_path),
            },
        )
        return repo

    def test_scan_finds_deprecated_annotations(self, tmp_path: Path) -> None:
        repo = self._make_repo(
            tmp_path,
            {
                "src/old.py": "@deprecated\ndef old_func(): pass",
                "src/new.py": "def new_func(): pass",
                "src/legacy.py": "import warnings\nwarnings.warn('Deprecated function', DeprecationWarning)\ndef legacy(): pass",
                "src/also_old.py": "@Deprecated\nclass OldClass: pass",
            },
        )
        result = scan_repo_for_family(repo, MIGRATION_INVENTORY)
        assert len(result.matched_files) >= 3
        assert "src/new.py" not in result.matched_files

    def test_scan_respects_min_hits(self, tmp_path: Path) -> None:
        repo = self._make_repo(
            tmp_path,
            {"src/one.py": "@deprecated\ndef f(): pass"},
        )
        # min_hits=3 default, only 1 file → should have hits but below threshold
        result = scan_repo_for_family(repo, MIGRATION_INVENTORY)
        assert len(result.matched_files) < MIGRATION_INVENTORY.min_hits

    def test_scan_compliance_audit(self, tmp_path: Path) -> None:
        repo = self._make_repo(
            tmp_path,
            {
                "pkg/server.go": 'import "crypto/tls"\nvar c tls.Config{}',
                "pkg/client.go": "var x = SSLContext()",
                "pkg/util.go": "func helper() {}",
                "config.yaml": "tls:\n  minVersion: 1.2",
            },
        )
        result = scan_repo_for_family(repo, COMPLIANCE_AUDIT)
        assert "pkg/server.go" in result.matched_files
        assert "pkg/util.go" not in result.matched_files


# ---------------------------------------------------------------------------
# Task generation tests
# ---------------------------------------------------------------------------


class TestGenerateOrgScaleTask:
    def _make_scan_result(self, tmp_path: Path) -> FamilyScanResult:
        return FamilyScanResult(
            family=MIGRATION_INVENTORY,
            hits=(
                PatternHit("src/old.py", 1, "@deprecated", r"@deprecated"),
                PatternHit("src/legacy.py", 3, "# Deprecated:", r"Deprecated:"),
                PatternHit("src/also.py", 1, "@Deprecated", r"@Deprecated"),
            ),
            repo_path=tmp_path,
            commit_sha="abc12345deadbeef",
            matched_files=frozenset({"src/old.py", "src/legacy.py", "src/also.py"}),
        )

    def test_no_llm_generates_deterministic_task(self, tmp_path: Path) -> None:
        scan = self._make_scan_result(tmp_path)
        task = generate_org_scale_task(scan, no_llm=True)

        assert task is not None
        assert task.metadata.org_scale is True
        assert task.metadata.category == "migration-inventory"
        assert task.verification.type == "oracle"
        assert task.verification.oracle_type == "file_list"
        assert len(task.verification.oracle_answer) == 3
        assert task.metadata.ground_truth_commit == "abc12345deadbeef"

    def test_multi_hop_task(self, tmp_path: Path) -> None:
        scan = self._make_scan_result(tmp_path)
        caller_files = frozenset({"src/consumer.py", "src/user.py", "src/caller.py"})
        task = generate_org_scale_task(scan, multi_hop_files=caller_files, no_llm=True)

        assert task is not None
        assert len(task.verification.oracle_answer) == 3
        # Multi-hop ground truth is the caller files, not the deprecated files
        assert "src/consumer.py" in task.verification.oracle_answer

    @patch("codeprobe.core.llm.call_claude")
    def test_llm_generates_task(self, mock_call: object, tmp_path: Path) -> None:
        from codeprobe.core.llm import LLMResponse

        mock_call.return_value = LLMResponse(
            text='{"heading": "Find deprecated APIs", '
            '"question": "Which files contain deprecated annotations?", '
            '"difficulty": "easy", "is_multi_hop": false}'
        )
        scan = self._make_scan_result(tmp_path)
        task = generate_org_scale_task(scan, no_llm=False)

        assert task is not None
        assert task.metadata.issue_title == "Find deprecated APIs"
        assert task.metadata.enrichment_source == "llm"

    @patch("codeprobe.core.llm.call_claude")
    def test_llm_failure_falls_back(self, mock_call: object, tmp_path: Path) -> None:
        from codeprobe.core.llm import LLMExecutionError

        mock_call.side_effect = LLMExecutionError("timeout")
        scan = self._make_scan_result(tmp_path)
        task = generate_org_scale_task(scan, no_llm=False)

        # Falls back to deterministic — still produces a task
        assert task is not None
        assert task.metadata.enrichment_source == ""


# ---------------------------------------------------------------------------
# Writer tests for oracle tasks
# ---------------------------------------------------------------------------


class TestWriteOracleTask:
    def test_write_oracle_task_dir(self, tmp_path: Path) -> None:
        task = Task(
            id="org12345",
            repo="myrepo",
            metadata=TaskMetadata(
                name="org-org12345",
                difficulty="medium",
                description="Find deprecated files",
                language="python",
                category="migration-inventory",
                org_scale=True,
                issue_title="Find deprecated APIs",
                issue_body="Which files contain deprecated annotations?",
                ground_truth_commit="abc123",
            ),
            verification=TaskVerification(
                type="oracle",
                command="bash tests/test.sh",
                reward_type="continuous",
                oracle_type="file_list",
                oracle_answer=("src/old.py", "src/legacy.py"),
            ),
        )
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "myrepo"

        result_path = write_task_dir(task, base_dir, repo_path)

        # Check instruction.md
        instruction = (result_path / "instruction.md").read_text()
        assert "Find deprecated APIs" in instruction
        assert "answer.txt" in instruction
        assert "Question" in instruction

        # Check ground_truth.json
        gt = json.loads((result_path / "ground_truth.json").read_text())
        assert gt["oracle_type"] == "file_list"
        assert set(gt["expected"]) == {"src/old.py", "src/legacy.py"}
        assert gt["commit"] == "abc123"

        # Check test.sh exists and is executable
        test_sh = result_path / "tests" / "test.sh"
        assert test_sh.exists()
        assert test_sh.stat().st_mode & 0o111  # executable

        # Check metadata.json
        meta = json.loads((result_path / "metadata.json").read_text())
        assert meta["metadata"]["org_scale"] is True
        assert meta["verification"]["type"] == "oracle"


# ---------------------------------------------------------------------------
# End-to-end integration test (premortem P0)
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def _make_repo(self, tmp_path: Path) -> Path:
        """Create a small repo with deprecated annotations for mining."""
        repo = tmp_path / "test-repo"
        repo.mkdir()
        (repo / "src").mkdir()
        (repo / "src" / "old_api.py").write_text(
            "@deprecated\ndef old_function():\n    pass\n"
        )
        (repo / "src" / "legacy.py").write_text(
            "import warnings\nwarnings.warn('Deprecated', DeprecationWarning)\ndef legacy():\n    pass\n"
        )
        (repo / "src" / "also_old.py").write_text(
            "@Deprecated\nclass OldClass:\n    pass\n"
        )
        (repo / "src" / "new_api.py").write_text("def new_function():\n    return 42\n")
        (repo / "src" / "consumer.py").write_text(
            "from src.old_api import old_function\nold_function()\n"
        )
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(
            ["git", "add", "."], cwd=str(repo), capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=str(repo),
            capture_output=True,
            check=True,
            env={
                "GIT_AUTHOR_NAME": "test",
                "GIT_AUTHOR_EMAIL": "test@test.com",
                "GIT_COMMITTER_NAME": "test",
                "GIT_COMMITTER_EMAIL": "test@test.com",
                "PATH": "/usr/bin:/bin",
                "HOME": str(tmp_path),
            },
        )
        return repo

    def test_mine_write_score_pipeline(self, tmp_path: Path) -> None:
        """E2E: mine org-scale tasks → write → simulate agent → oracle-check."""
        repo = self._make_repo(tmp_path)

        # Step 1: Mine tasks
        result = mine_org_scale_tasks(
            repo,
            count=2,
            families=(MIGRATION_INVENTORY,),
            no_llm=True,
        )
        assert len(result.tasks) >= 1

        task = result.tasks[0]
        assert task.metadata.org_scale is True
        assert task.verification.type == "oracle"
        assert len(task.verification.oracle_answer) >= 3

        # Step 2: Write task directory
        tasks_dir = tmp_path / "output" / "tasks"
        task_dir = write_task_dir(task, tasks_dir, repo)
        assert (task_dir / "instruction.md").exists()
        assert (task_dir / "ground_truth.json").exists()
        assert (task_dir / "tests" / "test.sh").exists()

        # Step 3: Simulate agent writing correct answer
        (task_dir / "answer.txt").write_text(
            "\n".join(task.verification.oracle_answer) + "\n"
        )

        # Step 4: Run oracle-check
        score_result = oracle_check(task_dir)
        assert score_result["f1"] == 1.0
        assert score_result["error"] == ""

    def test_mine_write_score_partial_answer(self, tmp_path: Path) -> None:
        """E2E: partial answer produces 0 < F1 < 1."""
        repo = self._make_repo(tmp_path)

        result = mine_org_scale_tasks(
            repo, count=1, families=(MIGRATION_INVENTORY,), no_llm=True
        )
        assert len(result.tasks) >= 1

        task = result.tasks[0]
        tasks_dir = tmp_path / "output" / "tasks"
        task_dir = write_task_dir(task, tasks_dir, repo)

        # Agent finds only 1 of N expected files
        first_file = list(task.verification.oracle_answer)[0]
        (task_dir / "answer.txt").write_text(first_file + "\n")

        score_result = oracle_check(task_dir)
        assert 0.0 < score_result["f1"] < 1.0
        assert score_result["recall"] < 1.0
        assert score_result["precision"] == 1.0

    def test_mine_write_score_wrong_answer(self, tmp_path: Path) -> None:
        """E2E: completely wrong answer produces F1 = 0."""
        repo = self._make_repo(tmp_path)

        result = mine_org_scale_tasks(
            repo, count=1, families=(MIGRATION_INVENTORY,), no_llm=True
        )
        task = result.tasks[0]
        tasks_dir = tmp_path / "output" / "tasks"
        task_dir = write_task_dir(task, tasks_dir, repo)

        (task_dir / "answer.txt").write_text("nonexistent/file.py\n")

        score_result = oracle_check(task_dir)
        assert score_result["f1"] == 0.0
