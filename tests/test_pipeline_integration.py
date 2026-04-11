"""Tests for pipeline-cli-integration: multi-repo, new flags, validation."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from codeprobe.cli import main
from codeprobe.mining.org_scale import mine_org_scale_tasks
from codeprobe.mining.org_scale_families import (
    FAMILIES,
    MIGRATION_INVENTORY,
)
from codeprobe.mining.org_scale_scanner import FamilyScanResult
from codeprobe.models.task import Task, TaskMetadata, TaskVerification

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@test.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@test.com",
    "PATH": "/usr/bin:/bin",
}


def _make_repo(
    tmp_path: Path, name: str = "repo", files: dict[str, str] | None = None
) -> Path:
    """Create a git repo with deprecated annotations for testing."""
    repo = tmp_path / name
    repo.mkdir()
    default_files = {
        "src/old.py": "@deprecated\ndef old_func(): pass",
        "src/legacy.py": "import warnings\nwarnings.warn('Deprecated', DeprecationWarning)\ndef legacy(): pass",
        "src/also_old.py": "@Deprecated\nclass OldClass: pass",
        "src/new.py": "def new_func(): return 42",
    }
    for path, content in (files or default_files).items():
        fp = repo / path
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
    env = {**_GIT_ENV, "HOME": str(tmp_path)}
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo),
        capture_output=True,
        check=True,
        env=env,
    )
    return repo


# ---------------------------------------------------------------------------
# AC1: mine_org_scale_tasks accepts repo_paths: list[Path]
# ---------------------------------------------------------------------------


class TestMultiRepoMining:
    def test_mine_with_single_repo_as_list(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        result = mine_org_scale_tasks(
            [repo],
            count=2,
            families=(MIGRATION_INVENTORY,),
            no_llm=True,
        )
        assert len(result.tasks) >= 1
        assert result.tasks[0].metadata.org_scale is True

    def test_mine_with_multiple_repos(self, tmp_path: Path) -> None:
        repo_a = _make_repo(tmp_path, "repo-a")
        repo_b = _make_repo(
            tmp_path,
            "repo-b",
            {
                "lib/old_api.py": "@deprecated\ndef old_api(): pass",
                "lib/compat.py": "@Deprecated\nclass Compat: pass",
                "lib/v2.py": "@deprecated\ndef v2_func(): pass",
            },
        )
        result = mine_org_scale_tasks(
            [repo_a, repo_b],
            count=3,
            families=(MIGRATION_INVENTORY,),
            no_llm=True,
        )
        assert len(result.tasks) >= 1

    def test_multi_repo_ground_truth_commits(self, tmp_path: Path) -> None:
        """AC7: multi-repo tasks include commits dict."""
        repo_a = _make_repo(tmp_path, "repo-a")
        repo_b = _make_repo(tmp_path, "repo-b")
        result = mine_org_scale_tasks(
            [repo_a, repo_b],
            count=2,
            families=(MIGRATION_INVENTORY,),
            no_llm=True,
        )
        assert len(result.tasks) >= 1
        task = result.tasks[0]
        # Multi-repo should have ground_truth_commits set
        assert len(task.metadata.ground_truth_commits) == 2
        repo_names = {name for name, _ in task.metadata.ground_truth_commits}
        assert "repo-a" in repo_names
        assert "repo-b" in repo_names


# ---------------------------------------------------------------------------
# AC2, AC3, AC4, AC10: CLI flag parsing
# ---------------------------------------------------------------------------


class TestCLIFlags:
    def test_help_shows_repos_flag(self) -> None:
        """AC10: --repos appears in advanced help."""
        runner = CliRunner()
        result = runner.invoke(main, ["mine", "--help", "--advanced"])
        assert result.exit_code == 0
        assert "--repos" in result.output

    def test_help_shows_scan_timeout_flag(self) -> None:
        """AC10: --scan-timeout appears in advanced help."""
        runner = CliRunner()
        result = runner.invoke(main, ["mine", "--help", "--advanced"])
        assert "--scan-timeout" in result.output

    def test_help_shows_validate_flag(self) -> None:
        """AC10: --validate appears in advanced help."""
        runner = CliRunner()
        result = runner.invoke(main, ["mine", "--help", "--advanced"])
        assert "--validate" in result.output

    @patch("codeprobe.mining.org_scale.mine_org_scale_tasks")
    @patch("codeprobe.cli.mine_cmd._is_interactive", return_value=False)
    def test_repos_flag_passes_multiple_paths(
        self, mock_interactive: MagicMock, mock_mine: MagicMock, tmp_path: Path
    ) -> None:
        """AC2: --repos passes multiple paths to scanner."""
        repo_a = _make_repo(tmp_path, "repo-a")
        repo_b = _make_repo(tmp_path, "repo-b")

        mock_mine.return_value = MagicMock(
            tasks=[],
            scan_results=[],
        )

        runner = CliRunner()
        runner.invoke(
            main,
            [
                "mine",
                str(repo_a),
                "--org-scale",
                "--no-llm",
                "--repos",
                str(repo_b),
            ],
        )
        # mine_org_scale_tasks should have been called with both paths
        assert mock_mine.called
        call_args = mock_mine.call_args
        repo_paths = call_args[0][0]  # first positional arg
        assert len(repo_paths) == 2

    @patch("codeprobe.mining.org_scale.mine_org_scale_tasks")
    @patch("codeprobe.cli.mine_cmd._is_interactive", return_value=False)
    def test_scan_timeout_flag(
        self, mock_interactive: MagicMock, mock_mine: MagicMock, tmp_path: Path
    ) -> None:
        """AC3: --scan-timeout passes value."""
        repo = _make_repo(tmp_path)
        mock_mine.return_value = MagicMock(tasks=[], scan_results=[])

        runner = CliRunner()
        runner.invoke(
            main,
            [
                "mine",
                str(repo),
                "--org-scale",
                "--no-llm",
                "--scan-timeout",
                "30",
            ],
        )
        assert mock_mine.called
        assert mock_mine.call_args[1].get("scan_timeout") == 30


# ---------------------------------------------------------------------------
# AC5: Interactive family selection
# ---------------------------------------------------------------------------


class TestInteractiveFamilySelection:
    @patch("codeprobe.mining.org_scale_scanner.scan_repo")
    @patch(
        "codeprobe.mining.org_scale_scanner.get_tracked_files", return_value=frozenset()
    )
    @patch("click.prompt", return_value="1")
    def test_interactive_selection_returns_family(
        self,
        mock_prompt: MagicMock,
        mock_tracked: MagicMock,
        mock_scan: MagicMock,
    ) -> None:
        from codeprobe.cli.mine_cmd import _interactive_family_selection

        mock_scan.return_value = []
        result = _interactive_family_selection([Path("/fake")])
        # Should return first family
        assert result is not None
        assert len(result) == 1
        assert result[0] == FAMILIES[0]

    @patch("codeprobe.mining.org_scale_scanner.scan_repo")
    @patch(
        "codeprobe.mining.org_scale_scanner.get_tracked_files", return_value=frozenset()
    )
    @patch("click.prompt", return_value="")
    def test_interactive_selection_empty_returns_none(
        self,
        mock_prompt: MagicMock,
        mock_tracked: MagicMock,
        mock_scan: MagicMock,
    ) -> None:
        from codeprobe.cli.mine_cmd import _interactive_family_selection

        mock_scan.return_value = []
        result = _interactive_family_selection([Path("/fake")])
        # Empty input means use all families
        assert result is None


# ---------------------------------------------------------------------------
# AC6: LLM ground truth validation (best-effort)
# ---------------------------------------------------------------------------


class TestLLMGroundTruthValidation:
    def _make_task(self) -> Task:
        return Task(
            id="test123",
            repo="myrepo",
            metadata=TaskMetadata(
                name="org-test123",
                category="migration-inventory",
                description="test task",
                issue_body="Find deprecated files",
                org_scale=True,
            ),
            verification=TaskVerification(
                type="oracle",
                oracle_type="file_list",
                oracle_answer=("src/old.py", "src/legacy.py", "src/also_old.py"),
            ),
        )

    @patch("codeprobe.core.llm.llm_available", return_value=False)
    def test_skips_when_llm_unavailable(
        self, mock_avail: MagicMock, tmp_path: Path
    ) -> None:
        from codeprobe.mining.org_scale import validate_ground_truth_sample

        task = self._make_task()
        result = validate_ground_truth_sample(task, [tmp_path])
        assert result is None

    @patch("codeprobe.core.llm.call_claude")
    @patch("codeprobe.core.llm.llm_available", return_value=True)
    @patch("codeprobe.mining.org_scale_scanner.get_tracked_files")
    def test_passes_when_llm_agrees(
        self,
        mock_tracked: MagicMock,
        mock_avail: MagicMock,
        mock_call: MagicMock,
        tmp_path: Path,
    ) -> None:
        from codeprobe.core.llm import LLMResponse
        from codeprobe.mining.org_scale import validate_ground_truth_sample

        mock_tracked.return_value = frozenset(
            {"src/old.py", "src/legacy.py", "src/also_old.py", "src/new.py"}
        )
        mock_call.return_value = LLMResponse(text='{"disagreements": []}')

        task = self._make_task()
        result = validate_ground_truth_sample(task, [tmp_path])
        assert result is True


# ---------------------------------------------------------------------------
# AC7: ground_truth_commits in writer output
# ---------------------------------------------------------------------------


class TestGroundTruthCommitsWriter:
    def test_multi_repo_commits_in_ground_truth_json(self, tmp_path: Path) -> None:
        from codeprobe.mining.writer import write_task_dir

        task = Task(
            id="multi123",
            repo="repo-a",
            metadata=TaskMetadata(
                name="org-multi123",
                category="migration-inventory",
                org_scale=True,
                issue_title="Find deprecated",
                issue_body="Which files?",
                ground_truth_commit="abc,def",
                ground_truth_commits=(("repo-a", "abc123"), ("repo-b", "def456")),
            ),
            verification=TaskVerification(
                type="oracle",
                command="bash tests/test.sh",
                oracle_type="file_list",
                oracle_answer=("src/old.py",),
            ),
        )
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "repo-a"
        repo_path.mkdir()

        task_dir = write_task_dir(task, base_dir, repo_path)
        gt = json.loads((task_dir / "ground_truth.json").read_text())
        assert "commits" in gt
        assert gt["commits"]["repo-a"] == "abc123"
        assert gt["commits"]["repo-b"] == "def456"

    def test_single_repo_no_commits_key(self, tmp_path: Path) -> None:
        from codeprobe.mining.writer import write_task_dir

        task = Task(
            id="single123",
            repo="myrepo",
            metadata=TaskMetadata(
                name="org-single123",
                category="migration-inventory",
                org_scale=True,
                issue_title="Find deprecated",
                issue_body="Which files?",
                ground_truth_commit="abc123",
            ),
            verification=TaskVerification(
                type="oracle",
                command="bash tests/test.sh",
                oracle_type="file_list",
                oracle_answer=("src/old.py",),
            ),
        )
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "myrepo"
        repo_path.mkdir()

        task_dir = write_task_dir(task, base_dir, repo_path)
        gt = json.loads((task_dir / "ground_truth.json").read_text())
        assert "commits" not in gt


# ---------------------------------------------------------------------------
# AC8: _run_org_scale_mine resolves URLs via _clone_repo
# ---------------------------------------------------------------------------


class TestRepoURLResolution:
    @patch("codeprobe.cli.mine_cmd._clone_repo")
    @patch("codeprobe.mining.org_scale.mine_org_scale_tasks")
    @patch("codeprobe.cli.mine_cmd._is_interactive", return_value=False)
    def test_repos_urls_resolved_via_clone(
        self,
        mock_interactive: MagicMock,
        mock_mine: MagicMock,
        mock_clone: MagicMock,
        tmp_path: Path,
    ) -> None:
        repo = _make_repo(tmp_path)
        cloned_path = tmp_path / "cloned"
        cloned_path.mkdir()
        mock_clone.return_value = cloned_path
        mock_mine.return_value = MagicMock(tasks=[], scan_results=[])

        runner = CliRunner()
        runner.invoke(
            main,
            [
                "mine",
                str(repo),
                "--org-scale",
                "--no-llm",
                "--repos",
                "https://github.com/org/repo.git",
            ],
        )
        mock_clone.assert_called_once_with("https://github.com/org/repo.git")


# ---------------------------------------------------------------------------
# AC3: scan_timeout forwarded through the call chain
# ---------------------------------------------------------------------------


class TestScanTimeoutForwarding:
    @patch("codeprobe.mining.org_scale.scan_repo")
    @patch("codeprobe.mining.org_scale.get_tracked_files", return_value=frozenset())
    @patch("codeprobe.mining.org_scale.get_head_sha", return_value="abc123")
    def test_scan_timeout_reaches_scan_repo(
        self,
        mock_sha: MagicMock,
        mock_tracked: MagicMock,
        mock_scan: MagicMock,
        tmp_path: Path,
    ) -> None:
        """scan_timeout passed to mine_org_scale_tasks reaches scan_repo as timeout_seconds."""
        mock_scan.return_value = []

        mine_org_scale_tasks(
            [tmp_path],
            count=1,
            families=(MIGRATION_INVENTORY,),
            no_llm=True,
            scan_timeout=30,
        )
        assert mock_scan.called
        call_kwargs = mock_scan.call_args[1]
        assert call_kwargs["timeout_seconds"] == 30

    def test_scan_repo_forwards_timeout_to_family_scanner(self) -> None:
        """scan_repo passes timeout_seconds through to scan_repo_for_family."""
        from codeprobe.mining.org_scale_scanner import scan_repo

        with patch(
            "codeprobe.mining.org_scale_scanner.scan_repo_for_family"
        ) as mock_family:
            mock_family.return_value = MagicMock(matched_files=[], hits=[])
            scan_repo(
                [Path("/fake")],
                (MIGRATION_INVENTORY,),
                timeout_seconds=42.0,
            )
            assert mock_family.called
            assert mock_family.call_args[1]["timeout_seconds"] == 42.0


# ---------------------------------------------------------------------------
# AC4: _run_validation constructs repo_paths correctly
# ---------------------------------------------------------------------------


class TestRunValidationRepoPaths:
    @patch("codeprobe.mining.org_scale_validate.validate_families")
    def test_repos_per_family_is_list_of_lists(self, mock_validate: MagicMock) -> None:
        """repos_per_family should be list[list[Path]], not flattened."""
        from codeprobe.cli.mine_cmd import _run_validation

        task = Task(
            id="val123",
            repo="myrepo",
            metadata=TaskMetadata(
                name="org-val123",
                category="migration-inventory",
                description="test",
                org_scale=True,
            ),
            verification=TaskVerification(
                type="oracle",
                oracle_type="file_list",
                oracle_answer=("src/old.py",),
            ),
        )
        scan_result = FamilyScanResult(
            family=MIGRATION_INVENTORY,
            hits=(),
            repo_paths=(Path("/repo-a"),),
            commit_sha="abc123",
            matched_files=frozenset({"src/old.py"}),
        )
        result = MagicMock(tasks=[task], scan_results=[scan_result])
        repo_paths = [Path("/repo-a"), Path("/repo-b")]

        mock_validate.return_value = []
        _run_validation(result, repo_paths)

        assert mock_validate.called
        call_args = mock_validate.call_args[0]
        repos_arg = call_args[2]  # third positional: repos_per_family
        # Should be list[list[Path]], each entry is the full repo_paths list
        assert len(repos_arg) == 1  # one family
        assert repos_arg[0] == repo_paths
        # NOT a flat list like [Path('/repo-a'), Path('/repo-b'), Path('/repo-a'), Path('/repo-b')]
        assert isinstance(repos_arg[0], list)
        assert all(isinstance(p, Path) for p in repos_arg[0])


# ---------------------------------------------------------------------------
# Budget warning visibility tests (phase0-budget-warning)
# ---------------------------------------------------------------------------


def _make_task_dir(base: Path, name: str, *, passing: bool = True) -> Path:
    """Create a minimal task directory with instruction and test.sh."""
    task_dir = base / name
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "instruction.md").write_text("Fix the bug.")
    tests_dir = task_dir / "tests"
    tests_dir.mkdir()
    test_sh = tests_dir / "test.sh"
    exit_code = 0 if passing else 1
    test_sh.write_text(f"#!/bin/bash\nexit {exit_code}\n")
    test_sh.chmod(test_sh.stat().st_mode | stat.S_IEXEC)
    return task_dir


class TestBudgetWarningVisibility:
    """Budget warnings must go to stderr and be visible without -v flag."""

    def test_budget_exceeded_message_on_stderr_sequential(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Budget exceeded prints to stderr in sequential mode (parallel=1)."""
        from codeprobe.adapters.protocol import AgentConfig
        from codeprobe.core.executor import execute_config
        from codeprobe.models.experiment import ExperimentConfig
        from tests.conftest import FakeAdapter

        tasks = [_make_task_dir(tmp_path, f"task-{i:03d}") for i in range(5)]
        adapter = FakeAdapter(stdout="output", cost_usd=0.05, cost_model="per_token")
        exp_config = ExperimentConfig(label="baseline")
        agent_config = AgentConfig()

        execute_config(
            adapter=adapter,
            task_dirs=tasks,
            repo_path=Path("/repo"),
            experiment_config=exp_config,
            agent_config=agent_config,
            max_cost_usd=0.10,
        )
        captured = capsys.readouterr()
        assert "Cost budget exceeded" in captured.err
        assert "$0.10" in captured.err
        assert "halting" in captured.err

    def test_80_pct_warning_on_stderr_sequential(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """80% budget warning prints to stderr in sequential mode."""
        from codeprobe.adapters.protocol import AgentConfig
        from codeprobe.core.executor import execute_config
        from codeprobe.models.experiment import ExperimentConfig
        from tests.conftest import SequentialCostAdapter

        tasks = [_make_task_dir(tmp_path, f"task-{i:03d}") for i in range(5)]
        # 5 tasks: costs $0.02, $0.02, $0.06, $0.02, $0.02
        # After task 2: cumulative = $0.04 (40% of $0.10)
        # After task 3: cumulative = $0.10 (100% of $0.10) -- triggers 80% warning
        # ... but also triggers budget exceeded (>= budget)
        # Use budget=$0.12 so 80% = $0.096
        # After task 3: cumulative = $0.10 (83%) -- triggers 80% warning
        adapter = SequentialCostAdapter(
            costs=[
                (0.02, "per_token"),
                (0.02, "per_token"),
                (0.06, "per_token"),
                (0.02, "per_token"),
                (0.02, "per_token"),
            ],
            stdout="output",
        )
        exp_config = ExperimentConfig(label="baseline")
        agent_config = AgentConfig()

        execute_config(
            adapter=adapter,
            task_dirs=tasks,
            repo_path=Path("/repo"),
            experiment_config=exp_config,
            agent_config=agent_config,
            max_cost_usd=0.12,
        )
        captured = capsys.readouterr()
        assert "Cost warning:" in captured.err
        assert "budget used" in captured.err

    def test_budget_exceeded_message_on_stderr_parallel(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Budget exceeded prints to stderr in parallel mode (parallel>1)."""
        from codeprobe.adapters.protocol import AgentConfig
        from codeprobe.core.executor import execute_config
        from codeprobe.models.experiment import ExperimentConfig
        from tests.conftest import FakeAdapter

        tasks = [_make_task_dir(tmp_path, f"task-{i:03d}") for i in range(5)]
        adapter = FakeAdapter(stdout="output", cost_usd=0.05, cost_model="per_token")
        exp_config = ExperimentConfig(label="baseline")
        agent_config = AgentConfig()

        # Use a fake isolation strategy that just returns tmp dirs
        class FakeIsolation:
            def acquire(self) -> Path:
                p = tmp_path / "worktree"
                p.mkdir(exist_ok=True)
                return p

            def release(self, path: Path) -> None:
                pass

            def cleanup(self) -> None:
                pass

        execute_config(
            adapter=adapter,
            task_dirs=tasks,
            repo_path=Path("/repo"),
            experiment_config=exp_config,
            agent_config=agent_config,
            max_cost_usd=0.10,
            parallel=2,
            isolation=FakeIsolation(),
        )
        captured = capsys.readouterr()
        assert "Cost budget exceeded" in captured.err
        assert "halting" in captured.err

    def test_80_pct_warning_emitted_only_once(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The 80% budget warning is emitted exactly once even with many tasks."""
        from codeprobe.adapters.protocol import AgentConfig
        from codeprobe.core.executor import execute_config
        from codeprobe.models.experiment import ExperimentConfig
        from tests.conftest import FakeAdapter

        tasks = [_make_task_dir(tmp_path, f"task-{i:03d}") for i in range(10)]
        # Each task costs $0.10, budget $1.00 -> 80% threshold at $0.80
        adapter = FakeAdapter(stdout="output", cost_usd=0.10, cost_model="per_token")
        exp_config = ExperimentConfig(label="baseline")
        agent_config = AgentConfig()

        execute_config(
            adapter=adapter,
            task_dirs=tasks,
            repo_path=Path("/repo"),
            experiment_config=exp_config,
            agent_config=agent_config,
            max_cost_usd=1.00,
        )
        captured = capsys.readouterr()
        assert captured.err.count("Cost warning:") == 1


# ---------------------------------------------------------------------------
# Sandbox thread-safety tests (phase0-budget-warning)
# ---------------------------------------------------------------------------


class TestSandboxThreadSafety:
    """os.environ['CODEPROBE_SANDBOX'] uses ref-counting, not raw set/pop."""

    def test_acquire_release_sandbox_refcount(self) -> None:
        """Ref-counting prevents early removal when multiple threads hold sandbox."""
        import codeprobe.cli.run_cmd as run_cmd
        from codeprobe.cli.run_cmd import (
            _acquire_sandbox,
            _release_sandbox,
        )

        # Clean state
        os.environ.pop("CODEPROBE_SANDBOX", None)
        run_cmd._sandbox_refcount = 0

        _acquire_sandbox()
        assert os.environ.get("CODEPROBE_SANDBOX") == "1"
        assert run_cmd._sandbox_refcount == 1

        _acquire_sandbox()
        assert os.environ.get("CODEPROBE_SANDBOX") == "1"
        assert run_cmd._sandbox_refcount == 2

        _release_sandbox()
        # Still held by one thread — env var should persist
        assert os.environ.get("CODEPROBE_SANDBOX") == "1"
        assert run_cmd._sandbox_refcount == 1

        _release_sandbox()
        # All released — env var should be gone
        assert os.environ.get("CODEPROBE_SANDBOX") is None
        assert run_cmd._sandbox_refcount == 0

    def test_concurrent_acquire_release_no_race(self) -> None:
        """Concurrent acquire/release does not corrupt refcount."""
        import codeprobe.cli.run_cmd as run_cmd
        from codeprobe.cli.run_cmd import _acquire_sandbox, _release_sandbox

        os.environ.pop("CODEPROBE_SANDBOX", None)
        run_cmd._sandbox_refcount = 0

        barrier = threading.Barrier(4)
        errors: list[str] = []

        def worker() -> None:
            try:
                barrier.wait(timeout=5)
                _acquire_sandbox()
                # Simulate work
                _release_sandbox()
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Thread errors: {errors}"
        assert run_cmd._sandbox_refcount == 0
        assert os.environ.get("CODEPROBE_SANDBOX") is None
