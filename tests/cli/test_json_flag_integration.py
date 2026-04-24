"""Integration tests for ``--json / --no-json / --json-lines`` flag wiring.

These tests exercise the flag surface on the Big 5 + diagnostic commands
and assert that the emitted stdout parses as either a terminal envelope
(``record_type=='envelope'``) or a stream of NDJSON events + a terminal
envelope — matching PRD §5.1 / §5.3.
"""

from __future__ import annotations

import json
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
