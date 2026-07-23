"""Integration tests for ``--json / --no-json / --json-lines`` flag wiring.

These tests exercise the flag surface on the Big 5 + diagnostic commands
and assert that the emitted stdout parses as either a terminal envelope
(``record_type=='envelope'``) or a stream of NDJSON events + a terminal
envelope — matching PRD §5.1 / §5.3.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli import main
from codeprobe.cli._output_helpers import resolve_mode


def _parse_last_json_line(output: str) -> dict:
    """Return the last non-empty stdout line parsed as JSON."""
    lines = [ln for ln in output.splitlines() if ln.strip()]
    assert lines, f"output was empty: {output!r}"
    return json.loads(lines[-1])


def _parse_single_envelope(output: str) -> dict:
    """Assert stdout is exactly one JSON line and return it parsed."""
    lines = [ln for ln in output.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected exactly one stdout line, got: {output!r}"
    payload = json.loads(lines[0])
    assert payload["record_type"] == "envelope"
    return payload


def _make_valid_task(parent: Path, name: str) -> Path:
    """Build a task directory that passes every structural check."""
    d = parent / name
    d.mkdir()
    (d / "instruction.md").write_text("# Task\nDo a thing.\n")
    (d / "task.toml").write_text(
        '[metadata]\nname = "t"\ntask_type = "sdlc_code_change"\n\n'
        '[verification]\nverification_mode = "test_script"\n'
    )
    tests_dir = d / "tests"
    tests_dir.mkdir()
    ts = tests_dir / "test.sh"
    ts.write_text("#!/bin/bash\nexit 0\n")
    ts.chmod(ts.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return d


def test_doctor_help_lists_all_three_json_flags() -> None:
    result = CliRunner().invoke(main, ["doctor", "--help"])
    assert result.exit_code == 0
    assert "--json" in result.output
    assert "--no-json" in result.output
    assert "--json-lines" in result.output


def test_doctor_json_emits_envelope() -> None:
    """``codeprobe doctor --json`` emits a single-line envelope."""
    result = CliRunner().invoke(main, ["doctor", "--json"])
    # doctor may exit 0 or 1 depending on the host env; we only assert
    # structural shape, not success.
    payload = _parse_last_json_line(result.output)
    assert payload["record_type"] == "envelope"
    assert payload["command"] == "doctor"
    assert payload["schema_version"] == "1"
    assert "ok" in payload
    assert "exit_code" in payload
    assert "data" in payload
    assert payload["data"]["command_schema_version"] == "1"
    assert "subsystem_status" in payload["data"]
    assert isinstance(payload["data"]["subsystem_status"], list)


def test_doctor_no_json_forces_pretty_even_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--no-json`` overrides ``CODEPROBE_JSON=1``."""
    monkeypatch.setenv("CODEPROBE_JSON", "1")
    result = CliRunner().invoke(main, ["doctor", "--no-json"])
    # Pretty output starts with "  PASS" / "  FAIL" lines — not JSON.
    # Accept any leading whitespace, just assert the first non-empty line
    # does NOT start with '{'.
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    assert lines
    assert not lines[0].lstrip().startswith("{"), (
        f"--no-json still produced JSON: {lines[0]!r}"
    )


def test_mine_json_emits_envelope_on_tmp_git_repo(tmp_path: Path) -> None:
    """``codeprobe mine --json`` on a fresh tmp repo emits an envelope."""
    # Create a minimal git repo with a single commit so mining has
    # SOMETHING to inspect. Yield count=0 tasks is acceptable — the
    # envelope structure is what matters.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"], cwd=tmp_path, check=True
    )
    (tmp_path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "init"], cwd=tmp_path, check=True
    )

    result = CliRunner().invoke(
        main,
        ["mine", str(tmp_path), "--no-interactive", "--no-llm", "--json"],
    )

    # Mining a fresh repo with no merged PRs may exit non-zero; we only
    # care that the terminal envelope, if emitted, is well-formed.
    # If exit is non-zero, skip — the error-migration unit covers error
    # envelopes. For success / zero-task paths we assert shape.
    if result.exit_code == 0:
        payload = _parse_last_json_line(result.output)
        assert payload["record_type"] == "envelope"
        assert payload["command"] == "mine"
        assert payload["data"]["command_schema_version"] == "1"


def test_interpret_json_on_minimal_experiment_emits_envelope(
    tmp_path: Path,
) -> None:
    """``codeprobe interpret --json`` on a results-less experiment."""
    # Build a minimal experiment.json with zero configs so
    # ``run_interpret`` takes the "No results found" branch.
    exp_dir = tmp_path / ".codeprobe"
    exp_dir.mkdir()
    (exp_dir / "experiment.json").write_text(
        json.dumps(
            {
                "name": "t1",
                "description": "",
                "tasks_dir": "tasks",
                "configs": [],
                "task_ids": [],
            }
        )
    )

    result = CliRunner().invoke(
        main, ["interpret", str(tmp_path), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = _parse_last_json_line(result.output)
    assert payload["record_type"] == "envelope"
    assert payload["command"] == "interpret"
    assert payload["data"]["command_schema_version"] == "1"
    assert payload["data"]["has_results"] is False


def test_resolve_mode_single_envelope_when_json_flag_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct unit test for the flag → mode mapping."""
    # Force non-TTY so TTY defaults don't interfere.
    import sys

    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

    mode = resolve_mode(
        "doctor",
        json_flag=True,
        no_json_flag=False,
        json_lines_flag=False,
    )
    assert mode.mode == "single_envelope"


def test_run_help_lists_json_flags() -> None:
    result = CliRunner().invoke(main, ["run", "--help"])
    assert result.exit_code == 0
    assert "--json" in result.output
    assert "--no-json" in result.output
    assert "--json-lines" in result.output


# ---------------------------------------------------------------------------
# validate --json (codeprobe-f7rl.15) — the exact invocation run's structured
# errors prescribe as diagnose_cmd must parse and emit an envelope.
# ---------------------------------------------------------------------------


def test_validate_json_single_task_passing() -> None:
    """``codeprobe validate <task_dir> --json`` emits one passing envelope."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        task = _make_valid_task(Path("."), "task-a")
        result = runner.invoke(main, ["validate", str(task), "--json"])
    assert result.exit_code == 0, result.output
    payload = _parse_single_envelope(result.output)
    assert payload["command"] == "validate"
    assert payload["ok"] is True
    assert payload["data"]["command_schema_version"] == "1"
    checks = payload["data"]["checks"]
    assert checks, "data.checks must be non-empty"
    assert all(c["passed"] for c in checks)
    assert payload["data"]["failed_count"] == 0
    assert payload["data"]["passed_count"] == len(checks)


def test_validate_json_single_task_failing(tmp_path: Path) -> None:
    """ok mirrors pass/fail: a broken task gives ok=false and exit 1."""
    # task.toml present (single-task shape) but instruction.md missing.
    (tmp_path / "task.toml").write_text('[metadata]\nname = "x"\n')
    result = CliRunner().invoke(main, ["validate", str(tmp_path), "--json"])
    assert result.exit_code == 1
    payload = _parse_single_envelope(result.output)
    assert payload["ok"] is False
    assert payload["exit_code"] == 1
    assert payload["data"]["failed_count"] >= 1
    failed = [c for c in payload["data"]["checks"] if not c["passed"]]
    assert any("instruction.md" in c["detail"] for c in failed)


def test_validate_json_parent_dir_mode(tmp_path: Path) -> None:
    """Parent-of-tasks mode emits per-task entries plus totals."""
    _make_valid_task(tmp_path, "good")
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "instruction.md").write_text("# Bad\n")

    result = CliRunner().invoke(main, ["validate", str(tmp_path), "--json"])
    assert result.exit_code == 1
    payload = _parse_single_envelope(result.output)
    assert payload["ok"] is False
    assert payload["data"]["total"] == 2
    assert payload["data"]["passed_count"] == 1
    assert payload["data"]["failed_count"] == 1
    by_id = {t["task_id"]: t for t in payload["data"]["tasks"]}
    assert by_id["good"]["passed"] is True
    assert by_id["good"]["failed_checks"] == []
    assert by_id["bad"]["passed"] is False
    assert by_id["bad"]["failed_checks"]


# ---------------------------------------------------------------------------
# experiment subcommands --json (codeprobe-f7rl.15)
# ---------------------------------------------------------------------------


def _init_experiment(runner: CliRunner, repo: Path) -> None:
    result = runner.invoke(
        main, ["experiment", "init", str(repo), "--non-interactive"]
    )
    assert result.exit_code == 0, result.output


def test_experiment_init_json_emits_envelope(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        main,
        ["experiment", "init", str(tmp_path), "--non-interactive", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = _parse_single_envelope(result.output)
    assert payload["command"] == "experiment init"
    assert payload["ok"] is True
    assert payload["data"]["name"] == "default"
    assert payload["data"]["created"] is True
    assert payload["data"]["experiment_dir"] == str(tmp_path / ".codeprobe")


def test_experiment_add_config_json_emits_envelope(tmp_path: Path) -> None:
    runner = CliRunner()
    _init_experiment(runner, tmp_path)
    result = runner.invoke(
        main,
        [
            "experiment",
            "add-config",
            str(tmp_path),
            "--label",
            "baseline",
            "--agent",
            "claude",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = _parse_single_envelope(result.output)
    assert payload["command"] == "experiment add-config"
    assert payload["data"]["label"] == "baseline"
    assert payload["data"]["agent"] == "claude"
    assert payload["data"]["config_count"] == 1


def test_experiment_status_json_emits_envelope(tmp_path: Path) -> None:
    runner = CliRunner()
    _init_experiment(runner, tmp_path)
    add = runner.invoke(
        main,
        ["experiment", "add-config", str(tmp_path), "--label", "baseline"],
    )
    assert add.exit_code == 0, add.output

    result = runner.invoke(
        main, ["experiment", "status", str(tmp_path), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = _parse_single_envelope(result.output)
    assert payload["command"] == "experiment status"
    assert payload["data"]["experiment"] == "default"
    assert payload["data"]["total_tasks"] == 0
    configs = payload["data"]["configs"]
    assert [c["label"] for c in configs] == ["baseline"]
    assert configs[0]["completed"] == 0
    assert configs[0]["status"] == "pending"


def test_experiment_validate_json_not_ready(tmp_path: Path) -> None:
    """validate --json on an empty experiment: ok=false, findings, exit 1."""
    runner = CliRunner()
    _init_experiment(runner, tmp_path)
    result = runner.invoke(
        main, ["experiment", "validate", str(tmp_path), "--json"]
    )
    assert result.exit_code == 1
    payload = _parse_single_envelope(result.output)
    assert payload["command"] == "experiment validate"
    assert payload["ok"] is False
    assert payload["data"]["status"] == "not_ready"
    assert any("No tasks" in e for e in payload["data"]["errors"])
    assert any("No configurations" in e for e in payload["data"]["errors"])


def test_experiment_aggregate_json_matches_report_file(tmp_path: Path) -> None:
    """aggregate --json embeds the exact reports/aggregate.json payload."""
    from codeprobe.core.experiment import save_config_results
    from codeprobe.models.experiment import CompletedTask

    runner = CliRunner()
    _init_experiment(runner, tmp_path)
    add = runner.invoke(
        main,
        ["experiment", "add-config", str(tmp_path), "--label", "baseline"],
    )
    assert add.exit_code == 0, add.output
    exp_dir = tmp_path / ".codeprobe"
    save_config_results(
        exp_dir,
        "baseline",
        [
            CompletedTask(
                task_id="task-001",
                automated_score=1.0,
                duration_seconds=2.0,
                cost_usd=0.10,
            ),
        ],
    )

    result = runner.invoke(
        main, ["experiment", "aggregate", str(tmp_path), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = _parse_single_envelope(result.output)
    assert payload["command"] == "experiment aggregate"

    report_path = exp_dir / "reports" / "aggregate.json"
    assert payload["data"]["report_path"] == str(report_path)
    on_disk = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["data"]["aggregate"] == on_disk


def test_experiment_add_config_duplicate_label_json_error(
    tmp_path: Path,
) -> None:
    """Duplicate --label with --json: typed envelope error, no traceback."""
    runner = CliRunner()
    _init_experiment(runner, tmp_path)
    first = runner.invoke(
        main,
        ["experiment", "add-config", str(tmp_path), "--label", "x"],
    )
    assert first.exit_code == 0, first.output

    result = runner.invoke(
        main,
        ["experiment", "add-config", str(tmp_path), "--label", "x", "--json"],
    )
    assert result.exit_code != 0
    payload = _parse_single_envelope(result.output)
    assert payload["ok"] is False
    assert payload["command"] == "experiment add-config"
    assert payload["error"]["code"] == "DUPLICATE_CONFIG_LABEL"
    assert payload["error"]["kind"] == "prescriptive"
    assert payload["error"]["next_try_flag"] == "--label"
    assert payload["error"]["next_try_value"] == "x-2"
    combined = result.output + (result.stderr or "")
    assert "Traceback" not in combined


def test_experiment_subcommands_help_list_json_flags() -> None:
    """Every experiment subcommand advertises the three JSON flags."""
    runner = CliRunner()
    for sub in ("init", "add-config", "validate", "status", "aggregate"):
        result = runner.invoke(main, ["experiment", sub, "--help"])
        assert result.exit_code == 0, result.output
        for flag in ("--json", "--no-json", "--json-lines"):
            assert flag in result.output, f"{sub} missing {flag}"


def test_check_infra_drift_json_emits_envelope(tmp_path: Path) -> None:
    """``check-infra drift --json`` emits a single envelope."""
    from codeprobe.mcp.capabilities import CAPABILITIES

    payload = {
        "id": "t1",
        "repo": "r",
        "metadata": {
            "name": "t",
            "mcp_capabilities_at_mine_time": sorted(CAPABILITIES.keys()),
        },
    }
    (tmp_path / "metadata.json").write_text(json.dumps(payload))

    result = CliRunner().invoke(
        main, ["check-infra", "drift", str(tmp_path), "--json"]
    )
    assert result.exit_code == 0, result.output
    parsed = _parse_last_json_line(result.output)
    assert parsed["record_type"] == "envelope"
    assert parsed["command"] == "check-infra drift"
    assert parsed["data"]["has_drift"] is False
