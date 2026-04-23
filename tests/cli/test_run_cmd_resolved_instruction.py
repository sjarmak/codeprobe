"""R6: persist resolved instruction alongside each task run.

Verifies that ``codeprobe run`` writes
``runs/{config}/{task_id}/instruction.resolved.md`` containing the byte-exact
prompt passed to the agent, and that an IO failure during that write aborts
the run (fail-loud per INV1 — no silent skip).
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.conftest import FakeAdapter


def _init_git_repo(repo: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "r@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "r"],
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("seed\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", "README.md"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "seed"],
        check=True,
        capture_output=True,
    )


def _setup_experiment(root: Path, *, instruction_text: str) -> tuple[Path, str]:
    """Create a minimal experiment in ``root/.codeprobe/exp``.

    Returns ``(exp_dir, task_id)``.
    """
    exp_dir = root / ".codeprobe" / "exp"
    tasks_dir = exp_dir / "tasks"
    task_id = "task-001"
    task_dir = tasks_dir / task_id
    task_dir.mkdir(parents=True)

    (task_dir / "instruction.md").write_text(instruction_text, encoding="utf-8")

    tests_dir = task_dir / "tests"
    tests_dir.mkdir()
    test_sh = tests_dir / "test.sh"
    test_sh.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    test_sh.chmod(test_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    experiment_json = {
        "name": "exp",
        "description": "r6 test",
        "tasks_dir": "tasks",
        "task_ids": [task_id],
        "configs": [
            {
                "label": "baseline",
                "agent": "fake",
                "model": None,
                "extra": {"timeout_seconds": 60},
            }
        ],
    }
    (exp_dir / "experiment.json").write_text(
        json.dumps(experiment_json), encoding="utf-8"
    )
    return exp_dir, task_id


def test_resolved_instruction_written_and_matches_agent_prompt(
    tmp_path: Path,
) -> None:
    """After a run, instruction.resolved.md equals the prompt given to the adapter."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    instruction_text = "Fix the KeyError in main.py."
    exp_dir, task_id = _setup_experiment(repo, instruction_text=instruction_text)

    adapter = FakeAdapter(stdout="ok", cost_usd=0.0, cost_model="unknown", duration=0.0)

    from codeprobe.cli import run_cmd as run_cmd_mod

    with patch.object(run_cmd_mod, "resolve", return_value=adapter):
        run_cmd_mod.run_eval(
            str(exp_dir),
            agent="fake",
            parallel=1,
            quiet=True,
            force_plain=True,
        )

    resolved = exp_dir / "runs" / "baseline" / task_id / "instruction.resolved.md"
    assert resolved.is_file(), f"expected {resolved} to exist"

    # Adapter must have been called with exactly the content of the file.
    assert adapter.run_calls, "FakeAdapter.run was never invoked"
    prompt_passed = adapter.run_calls[0][0]
    assert resolved.read_text(encoding="utf-8") == prompt_passed, (
        "instruction.resolved.md content must byte-exactly match prompt"
    )
    # Sanity: the instruction body is embedded in the resolved prompt.
    assert instruction_text in prompt_passed


def test_resolved_instruction_write_failure_aborts_run(
    tmp_path: Path,
) -> None:
    """A failing write propagates (fail-loud) — no silent skip."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    exp_dir, _ = _setup_experiment(repo, instruction_text="Do the thing.")

    adapter = FakeAdapter(stdout="ok", cost_usd=0.0, cost_model="unknown", duration=0.0)

    from codeprobe.cli import run_cmd as run_cmd_mod

    real_write_text = Path.write_text

    def failing_write_text(self: Path, *args: object, **kwargs: object) -> int:
        if self.name == "instruction.resolved.md":
            raise OSError("simulated IO failure")
        return real_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    with (
        patch.object(run_cmd_mod, "resolve", return_value=adapter),
        patch.object(Path, "write_text", failing_write_text),
        pytest.raises(OSError, match="simulated IO failure"),
    ):
        run_cmd_mod.run_eval(
            str(exp_dir),
            agent="fake",
            parallel=1,
            quiet=True,
            force_plain=True,
        )

    # Adapter must NOT have been invoked when the pre-write fails.
    assert not adapter.run_calls, (
        "adapter.run must not be called after instruction.resolved.md write fails"
    )
