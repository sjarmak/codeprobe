"""End-to-end integration tests for the typed-error migration.

These tests invoke the CLI through :class:`click.testing.CliRunner` so they
exercise the full surface: subcommand callback → ``CodeprobeError`` raise →
``CodeprobeGroup.invoke`` catch → envelope emission / pretty rendering →
``ctx.exit``.

CliRunner runs non-TTY by default, so the default output mode is
``single_envelope``. Tests that need the pretty branch force it via
``--no-json``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli import main


def _parse_envelope(output: str) -> dict:
    """Return the last JSON envelope line from stdout, parsed as dict."""
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("record_type") == "envelope":
            return payload
    raise AssertionError(f"no envelope line in output: {output!r}")


def _init_git_repo(repo: Path, *, seed_commit: bool = True) -> None:
    """Initialise a bare git repo with optional seed commit."""
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "t"],
        check=True,
        capture_output=True,
    )
    if seed_commit:
        (repo / "README.md").write_text("seed\n", encoding="utf-8")
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


# ---------------------------------------------------------------------------
# NO_EXPERIMENT — `codeprobe run` on a dir with no .codeprobe/
# ---------------------------------------------------------------------------


def test_no_experiment_produces_diagnostic_envelope(tmp_path: Path) -> None:
    """A nonexistent / uninitialised experiment path emits NO_EXPERIMENT."""
    # click.Path(exists=True) requires the dir to exist, so give it one that
    # has no .codeprobe/ inside.
    result = CliRunner().invoke(main, ["run", str(tmp_path), "--json"])
    assert result.exit_code == 2, result.output

    payload = _parse_envelope(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "NO_EXPERIMENT"
    assert payload["error"]["kind"] == "diagnostic"
    assert payload["error"]["terminal"] is True
    assert payload["error"]["message"]
    # next_steps list the Initialize action.
    assert any("init" in s["command"] for s in payload["next_steps"])


# ---------------------------------------------------------------------------
# INVALID_GIT_URL — `codeprobe mine` on a non-git directory
# ---------------------------------------------------------------------------


def test_mine_on_non_git_dir_produces_prescriptive_envelope(tmp_path: Path) -> None:
    """Running mine on a bare directory (no .git/) surfaces INVALID_GIT_URL."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    result = CliRunner().invoke(main, ["mine", str(plain), "--json"])
    assert result.exit_code == 2, result.output

    payload = _parse_envelope(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] in {"INVALID_GIT_URL", "CLONE_FAILED"}
    assert payload["error"]["kind"] == "prescriptive"
    # next_try_flag should be set for prescriptive errors.
    assert payload["error"]["next_try_flag"]


# ---------------------------------------------------------------------------
# NARRATIVE_SOURCE_UNDETECTABLE — direct fixture exercising _resolve_narrative_source
# ---------------------------------------------------------------------------


def test_narrative_source_undetectable_is_prescriptive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo with no PRs / no explicit --narrative-source raises PrescriptiveError.

    The helper is unit-tested directly because the full ``codeprobe mine``
    pipeline also does LLM / git work that is out of scope for a pure
    error-contract test.
    """
    from codeprobe.cli.errors import PrescriptiveError
    from codeprobe.cli.mine_cmd import _resolve_narrative_source

    # Force has_pr_narratives to return False.
    monkeypatch.setattr(
        "codeprobe.mining.sources.has_pr_narratives",
        lambda path: False,
    )

    with pytest.raises(PrescriptiveError) as excinfo:
        _resolve_narrative_source(
            narrative_source=(),
            repo_path=tmp_path,
            tasks_mined=True,
            pr_bodies={},
        )
    exc = excinfo.value
    assert exc.code == "NARRATIVE_SOURCE_UNDETECTABLE"
    assert exc.next_try_flag == "--narrative-source"
    assert exc.next_try_value == "commits"
    assert exc.terminal is False


# ---------------------------------------------------------------------------
# NO_TASKS — `codeprobe run` with experiment.json but empty tasks dir
# ---------------------------------------------------------------------------


def test_no_tasks_is_terminal(tmp_path: Path) -> None:
    """An initialised experiment with zero tasks emits NO_TASKS (terminal)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    exp_dir = repo / ".codeprobe"
    exp_dir.mkdir()
    experiment_json = {
        "name": "empty",
        "description": "",
        "tasks_dir": "tasks",
        "task_ids": [],
        "configs": [],
    }
    (exp_dir / "experiment.json").write_text(
        json.dumps(experiment_json), encoding="utf-8"
    )
    # Create the tasks_dir but leave it empty.
    (exp_dir / "tasks").mkdir()

    result = CliRunner().invoke(main, ["run", str(repo), "--json"])
    assert result.exit_code == 2, result.output

    payload = _parse_envelope(result.output)
    assert payload["error"]["code"] == "NO_TASKS"
    assert payload["error"]["kind"] == "diagnostic"
    assert payload["error"]["terminal"] is True
    # next_steps must include a mine command.
    commands = [step["command"] for step in payload["next_steps"]]
    assert any("mine" in cmd for cmd in commands), commands


# ---------------------------------------------------------------------------
# BUDGET_EXCEEDED — diagnostic, terminal, no next_try_flag
# ---------------------------------------------------------------------------


def test_budget_exceeded_has_no_next_try_flag() -> None:
    """BUDGET_EXCEEDED is diagnostic and MUST NOT auto-prescribe a flag bump."""
    from codeprobe.cli.errors import DiagnosticError

    # The PRD pins BUDGET_EXCEEDED as diagnostic (not prescriptive) so agents
    # can't auto-retry with a higher budget — that decision requires human
    # review of the previous run's behaviour.
    exc = DiagnosticError(
        code="BUDGET_EXCEEDED",
        message="Cost budget exceeded.",
        diagnose_cmd="codeprobe interpret <path> --format json",
        terminal=True,
    )

    # DiagnosticError has no next_try_flag attribute at all — enforce that
    # both by attribute lookup and by the envelope serialisation below.
    assert not hasattr(exc, "next_try_flag")
    assert exc.terminal is True

    from codeprobe.cli._error_handler import _error_payload

    payload = _error_payload(exc)
    assert payload.next_try_flag is None
    assert payload.next_try_value is None
    assert payload.diagnose_cmd == "codeprobe interpret <path> --format json"


# ---------------------------------------------------------------------------
# Handler rendering — pretty on TTY, envelope on non-TTY
# ---------------------------------------------------------------------------


def test_handler_renders_envelope_on_non_tty(tmp_path: Path) -> None:
    """On non-TTY (CliRunner default) an error surfaces as a JSON envelope."""
    # Using a dir without .codeprobe triggers NO_EXPERIMENT.
    result = CliRunner().invoke(main, ["run", str(tmp_path), "--json"])
    envelope = _parse_envelope(result.output)
    assert envelope["record_type"] == "envelope"
    assert envelope["ok"] is False


def test_handler_renders_pretty_when_no_json_forced(tmp_path: Path) -> None:
    """``--no-json`` forces the pretty stderr banner even on non-TTY."""
    # Older click versions fold stderr into result.output when mix_stderr
    # is not configurable. Either way, the pretty banner must be present
    # and the single-envelope JSON must NOT be emitted on stdout.
    result = CliRunner().invoke(main, ["run", str(tmp_path), "--no-json"])
    combined = (result.output or "") + (
        (result.stderr_bytes or b"").decode(errors="replace")
    )
    assert "ERROR" in combined
    assert "NO_EXPERIMENT" in combined
    # No envelope JSON line should exist in pretty mode.
    assert not any(
        line.strip().startswith('{"record_type":')
        for line in (result.output or "").splitlines()
    )


# ---------------------------------------------------------------------------
# Catalog-code coverage — every raised code is in error_codes.json
# ---------------------------------------------------------------------------


def test_every_migrated_code_is_in_catalog() -> None:
    """Defensive — the drift test already enforces this, but assert on a
    representative sample here too so a regression surfaces locally."""
    catalog_path = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "codeprobe"
        / "cli"
        / "error_codes.json"
    )
    data = json.loads(catalog_path.read_text(encoding="utf-8"))
    known = {entry["code"] for entry in data["codes"]}

    expected = {
        "NARRATIVE_SOURCE_UNDETECTABLE",
        "MUTEX_FLAGS",
        "INVALID_PERMISSION_MODE",
        "AMBIGUOUS_EXPERIMENT",
        "TRACE_BUDGET_EXCEEDED",
        "INVALID_GIT_URL",
        "UNKNOWN_BACKEND",
        "SOURCE_EXPORT_REQUIRES_ACK",
        "CANARY_PROOF_REQUIRED",
        "NO_EXPERIMENT",
        "NO_TASKS",
        "NO_SUITE_MATCH",
        "INTERRUPTED",
        "CANARY_PROOF_FAILED",
        "CANARY_MISMATCH",
        "CANARY_GATE_FAILED",
        "SNAPSHOT_CREATE_FAILED",
        "SNAPSHOT_VERIFY_FAILED",
        "METADATA_MISSING",
        "METADATA_INVALID",
        "CAPABILITY_DRIFT",
        "NO_BACKENDS_CONFIGURED",
        "OFFLINE_PREFLIGHT_FAILED",
        "CLONE_FAILED",
        "CALIBRATION_REJECTED",
        "DOCTOR_CHECKS_FAILED",
        "BUDGET_EXCEEDED",
    }
    missing = expected - known
    assert not missing, f"codes missing from catalog: {sorted(missing)}"
