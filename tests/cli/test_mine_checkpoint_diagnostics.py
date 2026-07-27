"""CLI diagnostics for checkpoint verifier failures during mining."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli import main, mine_cmd
from codeprobe.mining import multi_repo, writer
from codeprobe.models.task import Checkpoint, Task, TaskMetadata, TaskVerification


def _task_with_checkpoint_verifier(
    verifier: str = "missing-scope.sh",
) -> Task:
    return Task(
        id="missing-checkpoint-script",
        repo="example/primary",
        metadata=TaskMetadata(
            name="Missing checkpoint script",
            category="change-scope-audit",
            task_type="org_scale_cross_repo",
            language="python",
        ),
        verification=TaskVerification(
            type="oracle",
            command="bash tests/test.sh",
            verification_mode="artifact_eval",
            oracle_type="file_list",
            oracle_answer=("src/example.py",),
            checkpoints=(
                Checkpoint(
                    name="scope",
                    weight=1.0,
                    verifier=verifier,
                ),
            ),
        ),
    )


def test_mine_missing_checkpoint_verifier_is_coded_and_preserves_corpus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    primary.mkdir()
    secondary.mkdir()
    (primary / "example.py").write_text("value = 1\n", encoding="utf-8")
    existing = primary / ".codeprobe" / "tasks" / "existing-task"
    existing.mkdir(parents=True)
    sentinel = existing / "instruction.md"
    sentinel.write_text("keep this corpus\n", encoding="utf-8")

    monkeypatch.setattr(
        multi_repo,
        "mine_tasks_multi",
        lambda **_kwargs: multi_repo.MultiRepoMineResult(
            tasks=[_task_with_checkpoint_verifier()]
        ),
    )
    monkeypatch.setattr(mine_cmd, "_resolve_repo_path", lambda path: Path(path))

    result = CliRunner().invoke(
        main,
        [
            "mine",
            str(primary),
            "--cross-repo",
            str(secondary),
            "--backend",
            "ast",
            "--count",
            "1",
            "--no-interactive",
            "--no-llm",
            "--json",
        ],
    )

    assert result.exit_code == 2, result.output
    payload = json.loads(
        next(line for line in reversed(result.output.splitlines()) if line.strip())
    )
    assert payload["error"]["code"] == "MISSING_CHECKPOINT_VERIFIER"
    assert payload["error"]["kind"] == "diagnostic"
    assert payload["error"]["terminal"] is True
    assert "missing-scope.sh" in payload["error"]["message"]
    assert any(
        "missing-scope.sh" in step["summary"] for step in payload["next_steps"]
    )
    assert sentinel.read_text(encoding="utf-8") == "keep this corpus\n"


@pytest.mark.parametrize("kind", ["nul", "overlong"])
def test_mine_preflights_destination_filename_before_corpus_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    primary.mkdir()
    secondary.mkdir()
    existing = primary / ".codeprobe" / "tasks" / "existing-task"
    existing.mkdir(parents=True)
    sentinel = existing / "instruction.md"
    sentinel.write_text("keep this corpus\n", encoding="utf-8")
    name_max = os.pathconf(primary, "PC_NAME_MAX")
    verifier = "bad\0.sh" if kind == "nul" else "v" * (name_max + 1)
    task = _task_with_checkpoint_verifier(verifier)

    monkeypatch.setattr(
        multi_repo,
        "mine_tasks_multi",
        lambda **_kwargs: multi_repo.MultiRepoMineResult(tasks=[task]),
    )
    monkeypatch.setattr(mine_cmd, "_resolve_repo_path", lambda path: Path(path))
    monkeypatch.setattr(
        writer,
        "resolve_checkpoint_scripts",
        lambda _task: {verifier: "#!/bin/bash\nexit 1\n"},
    )

    result = CliRunner().invoke(
        main,
        [
            "mine",
            str(primary),
            "--cross-repo",
            str(secondary),
            "--backend",
            "ast",
            "--count",
            "1",
            "--no-interactive",
            "--no-llm",
            "--json",
        ],
    )

    assert result.exit_code == 2, result.output
    payload = json.loads(
        next(line for line in reversed(result.output.splitlines()) if line.strip())
    )
    assert payload["error"]["code"] == "MISSING_CHECKPOINT_VERIFIER"
    assert sentinel.read_text(encoding="utf-8") == "keep this corpus\n"


def test_refresh_read_failure_uses_checkpoint_diagnostic_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    task = _task_with_checkpoint_verifier("refresh.sh")
    task_dir = writer.write_task_dir(
        task,
        tmp_path / "tasks",
        repo_path,
        checkpoint_scripts={"refresh.sh": "#!/bin/bash\nexit 1\n"},
    )
    (task_dir / "tests" / "verifiers" / "refresh.sh").unlink()
    monkeypatch.setattr(mine_cmd, "_resolve_repo_path", lambda _path: repo_path)
    monkeypatch.setattr(
        mine_cmd,
        "_resolve_refresh_commit",
        lambda _repo: "bbb1111",
    )

    result = CliRunner().invoke(
        main,
        [
            "mine",
            str(repo_path),
            "--refresh",
            str(task_dir),
            "--no-interactive",
            "--json",
        ],
    )

    assert result.exit_code == 2, result.output
    payload = json.loads(
        next(line for line in reversed(result.output.splitlines()) if line.strip())
    )
    assert payload["error"]["code"] == "MISSING_CHECKPOINT_VERIFIER"
    assert "refresh.sh" in payload["error"]["message"]
