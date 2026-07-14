"""``codeprobe interpret`` must EXIT non-zero when the validity gate FAILs.

codeprobe-c4al (follow-up to codeprobe-77z). The infra-failure validity gate
(``Report.validity``) was rendered but never enforced: ``run_interpret``
returned 0 whether the gate PASSed or FAILed, so an agent-driven pipeline
branching on the exit code read "success" and treated an unquotable mean as
quotable. These tests pin the gate in both directions.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from click.testing import CliRunner

from codeprobe.cli import main
from codeprobe.models.experiment import CompletedTask


def _write_experiment(exp_dir: Path, tasks: list[CompletedTask]) -> None:
    """Materialize a one-config experiment whose single arm holds *tasks*."""
    exp_dir.mkdir(parents=True, exist_ok=True)
    (exp_dir / "experiment.json").write_text(
        json.dumps(
            {
                "name": "gate-exp",
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
    """The 0d4ec3ad-class casualty: token-ceiling overrun, empty scoring."""
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


def test_interpret_exits_nonzero_when_validity_fails(tmp_path: Path) -> None:
    """A run holding an unresolved infra casualty is NOT a successful exit."""
    _write_experiment(
        tmp_path / ".codeprobe", [_valid_trial(), _infra_trial()]
    )

    result = CliRunner().invoke(main, ["interpret", str(tmp_path), "--json"])

    assert result.exit_code == 2, result.output
    payload = _last_json_line(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "VALIDITY_FAILED"
    assert payload["error"]["terminal"] is True
    # The verdict — including the offending trial ids — must survive into the
    # error payload so an agent can act without re-parsing prose.
    assert payload["error"]["detail"]["infra_failure_count"] == 1
    assert payload["error"]["detail"]["infra_failure_trial_ids"] == ["t-crash#rep0"]


def test_interpret_failure_envelope_still_carries_the_report(
    tmp_path: Path,
) -> None:
    """Failing the gate must not cost the caller the report payload."""
    _write_experiment(
        tmp_path / ".codeprobe", [_valid_trial(), _infra_trial()]
    )

    result = CliRunner().invoke(main, ["interpret", str(tmp_path), "--json"])

    assert result.exit_code == 2, result.output
    payload = _last_json_line(result.output)
    assert payload["ok"] is False
    data = payload["data"]
    assert data["has_results"] is True
    assert data["validity"]["passed"] is False
    assert data["report"]["validity"]["infra_failure_count"] == 1
    # Exactly one envelope — the error path must replace the success emit,
    # not follow it.
    envelopes = [
        ln
        for ln in result.output.strip().splitlines()
        if ln.strip().startswith("{") and '"record_type": "envelope"' in ln
    ]
    assert len(envelopes) == 1, result.output


def test_interpret_exits_zero_when_validity_passes(tmp_path: Path) -> None:
    """The gate only fires on FAIL — a clean run still exits 0."""
    _write_experiment(
        tmp_path / ".codeprobe", [_valid_trial("t1"), _valid_trial("t2")]
    )

    result = CliRunner().invoke(main, ["interpret", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = _last_json_line(result.output)
    assert payload["ok"] is True
    assert payload["data"]["validity"]["passed"] is True


def test_interpret_pretty_mode_prints_report_then_exits_nonzero(
    tmp_path: Path,
) -> None:
    """Pretty mode keeps the report on stdout; the gate only changes the exit."""
    _write_experiment(
        tmp_path / ".codeprobe", [_valid_trial(), _infra_trial()]
    )

    result = CliRunner().invoke(main, ["interpret", str(tmp_path), "--no-json"])

    assert result.exit_code == 2, result.output
    assert "VALIDITY FAIL" in result.stdout
    assert "VALIDITY_FAILED" in result.stderr
