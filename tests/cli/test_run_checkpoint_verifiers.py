"""Run preflight rejects checkpoints without real verifier scripts."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from codeprobe.cli.errors import DiagnosticError
from codeprobe.cli.run_cmd import _check_checkpoint_verifiers_present


def _make_task(
    parent: Path,
    name: str,
    *,
    verifier: str,
    declare_in_metadata: bool = True,
) -> Path:
    task_dir = parent / name
    task_dir.mkdir()
    (task_dir / "instruction.md").write_text("# Task\n", encoding="utf-8")
    checkpoint_block = (
        textwrap.dedent(
            f"""\

            [[checkpoints]]
            name = "answer"
            weight = 1.0
            verifier = "{verifier}"
            """
        )
        if declare_in_metadata
        else ""
    )
    (task_dir / "task.toml").write_text(
        textwrap.dedent(
            f"""\
            [task]
            id = "{name}"
            repo = "test/repo"

            [metadata]
            name = "{name}"

            [verification]
            type = "test_script"
            reward_type = "weighted_checkpoints"
            """
        )
        + checkpoint_block,
        encoding="utf-8",
    )
    return task_dir


def _write_verifier(task_dir: Path, name: str, body: str) -> None:
    verifier = task_dir / "tests" / "verifiers" / name
    verifier.parent.mkdir(parents=True, exist_ok=True)
    verifier.write_text(body, encoding="utf-8")


def test_missing_checkpoint_verifier_fails_preflight(tmp_path: Path) -> None:
    task_dir = _make_task(tmp_path, "missing", verifier="missing.sh")

    with pytest.raises(DiagnosticError) as exc_info:
        _check_checkpoint_verifiers_present([task_dir], str(tmp_path))

    error = exc_info.value
    assert error.code == "MISSING_CHECKPOINT_VERIFIER"
    assert error.detail["checkpoint_verifier_problems"] == [
        {"task": "missing", "verifier": "missing.sh", "reason": "missing"}
    ]


def test_historical_exit_zero_stub_fails_preflight(tmp_path: Path) -> None:
    task_dir = _make_task(tmp_path, "stubbed", verifier="answer.sh")
    _write_verifier(
        task_dir,
        "answer.sh",
        "#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n",
    )

    with pytest.raises(DiagnosticError) as exc_info:
        _check_checkpoint_verifiers_present([task_dir], str(tmp_path))

    error = exc_info.value
    assert error.code == "MISSING_CHECKPOINT_VERIFIER"
    assert error.detail["checkpoint_verifier_problems"] == [
        {"task": "stubbed", "verifier": "answer.sh", "reason": "stub"}
    ]


def test_metadata_checkpoints_take_precedence_over_json(tmp_path: Path) -> None:
    task_dir = _make_task(tmp_path, "precedence", verifier="metadata.sh")
    _write_verifier(
        task_dir,
        "metadata.sh",
        '#!/usr/bin/env bash\nprintf \'{"score": 1.0}\\n\'\n',
    )
    checkpoints_file = task_dir / "tests" / "checkpoints.json"
    checkpoints_file.write_text(
        json.dumps(
            [{"name": "legacy", "weight": 1.0, "verifier": "missing-json.sh"}]
        ),
        encoding="utf-8",
    )

    _check_checkpoint_verifiers_present([task_dir], str(tmp_path))


def test_checkpoints_json_is_used_without_metadata_declarations(
    tmp_path: Path,
) -> None:
    task_dir = _make_task(
        tmp_path,
        "json-fallback",
        verifier="unused.sh",
        declare_in_metadata=False,
    )
    checkpoints_file = task_dir / "tests" / "checkpoints.json"
    checkpoints_file.parent.mkdir(parents=True)
    checkpoints_file.write_text(
        json.dumps(
            [{"name": "legacy", "weight": 1.0, "verifier": "missing-json.sh"}]
        ),
        encoding="utf-8",
    )

    with pytest.raises(DiagnosticError) as exc_info:
        _check_checkpoint_verifiers_present([task_dir], str(tmp_path))

    assert exc_info.value.detail["checkpoint_verifier_problems"] == [
        {
            "task": "json-fallback",
            "verifier": "missing-json.sh",
            "reason": "missing",
        }
    ]


def test_metadata_json_checkpoint_is_checked(tmp_path: Path) -> None:
    task_dir = tmp_path / "metadata-json"
    task_dir.mkdir()
    (task_dir / "instruction.md").write_text("# Task\n", encoding="utf-8")
    (task_dir / "metadata.json").write_text(
        json.dumps(
            {
                "metadata": {"name": "metadata-json"},
                "verification": {
                    "reward_type": "weighted_checkpoints",
                    "checkpoints": [
                        {
                            "name": "answer",
                            "weight": 1.0,
                            "verifier": "missing-metadata-json.sh",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(DiagnosticError) as exc_info:
        _check_checkpoint_verifiers_present([task_dir], str(tmp_path))

    assert exc_info.value.detail["checkpoint_verifier_problems"] == [
        {
            "task": "metadata-json",
            "verifier": "missing-metadata-json.sh",
            "reason": "missing",
        }
    ]
