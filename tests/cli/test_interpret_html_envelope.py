"""``codeprobe interpret --format html`` must write the file in EVERY mode.

codeprobe-f7rl.32. The envelope branch of ``run_interpret`` ignored ``fmt``
entirely, so a non-TTY caller (any agent driving the CLI, any pipe) asking
for ``--format html`` got a JSON envelope and no HTML file at all — the MVP
deliverable silently failed to materialize unless the operator knew to add
``--no-json``. These tests pin the new contract: html always writes the
file, and the envelope carries ``data.html_report_path`` so the artifact is
discoverable, including on a VALIDITY_FAILED exit.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from click.testing import CliRunner

from codeprobe.cli import main
from codeprobe.models.experiment import CompletedTask

EXPERIMENT_NAME = "html-exp"


def _write_experiment(exp_dir: Path, tasks: list[CompletedTask]) -> None:
    """Materialize a one-config experiment whose single arm holds *tasks*."""
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


def _infra_trial(task_id: str = "t-crash") -> CompletedTask:
    return CompletedTask(
        task_id=task_id,
        automated_score=0.0,
        status="error",
        error_category="system",
        metadata={"error": "API Error: exceeded the 32000 output token maximum"},
    )


def _last_json_line(output: str) -> dict:
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise AssertionError(f"no JSON line in output:\n{output}")


def _report_file(tmp_path: Path) -> Path:
    return tmp_path / ".codeprobe" / f"{EXPERIMENT_NAME}_report.html"


def test_envelope_mode_writes_html_file(tmp_path: Path) -> None:
    """Non-TTY (CliRunner default) --format html writes the file AND the envelope."""
    _write_experiment(tmp_path / ".codeprobe", [_valid_trial("t1"), _valid_trial("t2")])

    result = CliRunner().invoke(
        main, ["interpret", str(tmp_path), "--format", "html"]
    )

    assert result.exit_code == 0, result.output
    out_path = _report_file(tmp_path)
    assert out_path.is_file(), f"HTML report not written to {out_path}"
    assert out_path.read_text().strip()
    payload = _last_json_line(result.output)
    assert payload["ok"] is True
    assert payload["data"]["html_report_path"] == str(out_path)


def test_envelope_html_path_present_on_validity_failure(tmp_path: Path) -> None:
    """A FAILED-validity run still writes the file and reports its path."""
    _write_experiment(tmp_path / ".codeprobe", [_valid_trial(), _infra_trial()])

    result = CliRunner().invoke(
        main, ["interpret", str(tmp_path), "--format", "html"]
    )

    assert result.exit_code == 2, result.output
    out_path = _report_file(tmp_path)
    assert out_path.is_file(), f"HTML report not written to {out_path}"
    payload = _last_json_line(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "VALIDITY_FAILED"
    assert payload["data"]["html_report_path"] == str(out_path)


def test_pretty_mode_html_unchanged(tmp_path: Path) -> None:
    """--no-json --format html: file written, echo present, no envelope."""
    _write_experiment(tmp_path / ".codeprobe", [_valid_trial("t1"), _valid_trial("t2")])

    result = CliRunner().invoke(
        main, ["interpret", str(tmp_path), "--no-json", "--format", "html"]
    )

    assert result.exit_code == 0, result.output
    out_path = _report_file(tmp_path)
    assert out_path.is_file(), f"HTML report not written to {out_path}"
    assert f"HTML report written to {out_path}" in result.stdout
    envelopes = [
        ln
        for ln in result.output.strip().splitlines()
        if ln.strip().startswith("{") and '"record_type": "envelope"' in ln
    ]
    assert not envelopes, result.output


def test_envelope_mode_without_html_format_writes_nothing(tmp_path: Path) -> None:
    """Envelope run without --format html creates no report file.

    Regression guard against always-writing. Note the click default
    ``--format text`` maps to pretty mode even in non-TTY contexts, so the
    envelope-without-format shape is pinned via ``--json``.
    """
    _write_experiment(tmp_path / ".codeprobe", [_valid_trial("t1"), _valid_trial("t2")])

    result = CliRunner().invoke(main, ["interpret", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.output
    html_files = list((tmp_path / ".codeprobe").glob("*_report.html"))
    assert html_files == [], html_files
    payload = _last_json_line(result.output)
    assert payload["ok"] is True
    assert "html_report_path" not in payload["data"]
