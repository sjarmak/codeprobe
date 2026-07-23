"""Tests for the ``--out`` flag on ``codeprobe interpret`` (codeprobe-xcue).

BUG-OUT-FLAG-002 requires ``mine``/``run``/``interpret`` to all accept
``--out`` for custom output paths. This module covers ``interpret``: the
``--help`` surface check plus functional redirection of the written report
(HTML by default; --out also materializes text/json/csv reports which
otherwise write nothing to disk), and confirms omitting the flag preserves
the pre-existing default behavior exactly.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from click.testing import CliRunner

from codeprobe.cli import main
from codeprobe.models.experiment import CompletedTask

EXPERIMENT_NAME = "out-flag-exp"


def _write_experiment(exp_dir: Path, tasks: list[CompletedTask]) -> None:
    exp_dir.mkdir(parents=True, exist_ok=True)
    (exp_dir / "experiment.json").write_text(
        json.dumps(
            {
                "name": EXPERIMENT_NAME,
                "description": "",
                "tasks_dir": "tasks",
                "configs": [{"label": "arm-a", "agent": "claude"}],
                "task_ids": [t.task_id for t in tasks],
            }
        )
    )
    run_dir = exp_dir / "runs" / "arm-a"
    run_dir.mkdir(parents=True)
    (run_dir / "results.json").write_text(
        json.dumps(
            {
                "config": "arm-a",
                "completed": [asdict(t) for t in tasks],
            }
        )
    )


def _valid_trial(task_id: str = "t-ok") -> CompletedTask:
    return CompletedTask(
        task_id=task_id,
        automated_score=0.8,
        status="completed",
        scoring_details={"passed": True},
    )


def _default_html_path(tmp_path: Path) -> Path:
    return tmp_path / ".codeprobe" / f"{EXPERIMENT_NAME}_report.html"


def test_interpret_help_contains_out() -> None:
    """BUG-OUT-FLAG-002: `codeprobe interpret --help` must advertise --out."""
    result = CliRunner().invoke(main, ["interpret", "--help"])
    assert result.exit_code == 0, result.output
    assert "--out" in result.output


def test_omitting_out_keeps_default_html_location(tmp_path: Path) -> None:
    """Default behavior (--format html, no --out) is unchanged."""
    _write_experiment(tmp_path / ".codeprobe", [_valid_trial("t1"), _valid_trial("t2")])

    result = CliRunner().invoke(
        main, ["interpret", str(tmp_path), "--format", "html"]
    )
    assert result.exit_code == 0, result.output

    default_path = _default_html_path(tmp_path)
    assert default_path.is_file()
    assert default_path.read_text().strip()


def test_omitting_out_writes_nothing_for_text_format(tmp_path: Path) -> None:
    """Default behavior (--format text, no --out) writes no report file."""
    exp_dir = tmp_path / ".codeprobe"
    _write_experiment(exp_dir, [_valid_trial("t1")])

    result = CliRunner().invoke(main, ["interpret", str(tmp_path)])
    assert result.exit_code == 0, result.output

    assert not _default_html_path(tmp_path).exists()
    # No stray report file materialized anywhere under the experiment dir.
    assert not list(exp_dir.glob("*_report.*"))


def test_out_redirects_html_report(tmp_path: Path) -> None:
    """--out overrides the default HTML write location."""
    _write_experiment(tmp_path / ".codeprobe", [_valid_trial("t1"), _valid_trial("t2")])
    out_file = tmp_path / "custom-report.html"

    result = CliRunner().invoke(
        main,
        [
            "interpret",
            str(tmp_path),
            "--format",
            "html",
            "--out",
            str(out_file),
        ],
    )
    assert result.exit_code == 0, result.output

    assert out_file.is_file()
    assert out_file.read_text().strip()
    assert not _default_html_path(tmp_path).exists()

    payload = json.loads(
        [ln for ln in result.output.splitlines() if ln.strip()][-1]
    )
    assert payload["data"]["html_report_path"] == str(out_file)
    assert payload["data"]["out_path"] == str(out_file)


def test_out_materializes_json_report_to_disk(tmp_path: Path) -> None:
    """--out with a non-html format writes the rendered report to disk too
    (the default, --out-less behavior for those formats writes nothing)."""
    _write_experiment(tmp_path / ".codeprobe", [_valid_trial("t1")])
    out_file = tmp_path / "custom-report.json"

    result = CliRunner().invoke(
        main,
        [
            "interpret",
            str(tmp_path),
            "--format",
            "json",
            "--out",
            str(out_file),
            "--no-json",
        ],
    )
    assert result.exit_code == 0, result.output

    assert out_file.is_file()
    written = json.loads(out_file.read_text())
    assert written["experiment_name"] == EXPERIMENT_NAME
    assert f"Report written to {out_file}" in result.stdout


def test_out_rejects_missing_parent_directory(tmp_path: Path) -> None:
    _write_experiment(tmp_path / ".codeprobe", [_valid_trial("t1")])
    bad_out = tmp_path / "does-not-exist" / "report.html"

    result = CliRunner().invoke(
        main,
        [
            "interpret",
            str(tmp_path),
            "--format",
            "html",
            "--out",
            str(bad_out),
        ],
    )
    assert result.exit_code != 0
    assert "does not exist" in result.output
