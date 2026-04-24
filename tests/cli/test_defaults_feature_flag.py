"""End-to-end feature-flag tests for gate-on-context defaults.

Verifies that ``CODEPROBE_DEFAULTS=v0.7`` activates the resolver-driven
default behavior and that unset / ``v0.6`` keep behavior unchanged.

Also exercises the ``doctor --json --compact`` ≤2 KB envelope budget
used for SKILL.md preflight substitution.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli import main
from codeprobe.config.defaults import (
    CODEPROBE_DEFAULTS_ENV,
    compact_budget_bytes,
)

# ---------------------------------------------------------------------------
# doctor --json --compact
# ---------------------------------------------------------------------------


def test_doctor_compact_envelope_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compact JSON envelope must fit inside the SKILL.md 2 KB budget."""
    # Keep the environment lean so the envelope has minimal variance.
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    for key in ("SOURCEGRAPH_TOKEN", "SRC_ACCESS_TOKEN", "SOURCEGRAPH_ACCESS_TOKEN"):
        monkeypatch.delenv(key, raising=False)

    runner = CliRunner()
    result = runner.invoke(main, ["doctor", "--json", "--compact"])

    # Non-zero exit is fine — we're testing envelope size, not PASS/FAIL.
    assert result.exit_code in (0, 1)
    payload = result.output.strip()
    assert payload, "expected JSON payload on stdout"
    # Most recent non-empty line is the envelope (some text may precede
    # when checks log errors, but not with --json).
    envelope_line = payload.splitlines()[-1]
    envelope = json.loads(envelope_line)

    assert envelope["record_type"] == "doctor"
    assert envelope["command"] == "doctor"
    assert "tenant" in envelope["data"]
    assert "llm_available" in envelope["data"]
    assert "gh_auth_ok" in envelope["data"]
    assert "sourcegraph_token_present" in envelope["data"]

    size = len(envelope_line.encode("utf-8"))
    assert size <= compact_budget_bytes(), (
        f"compact envelope is {size} bytes, budget is {compact_budget_bytes()}"
    )


def test_doctor_full_json_not_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without --compact, the envelope has subsystem_status and may exceed 2KB."""
    runner = CliRunner()
    result = runner.invoke(main, ["doctor", "--json"])
    # 0 on all-pass; 2 when checks fail (DiagnosticError exit_code via the
    # typed-error migration). Pre-migration doctor used 1.
    assert result.exit_code in (0, 2)
    envelope_line = result.output.strip().splitlines()[-1]
    envelope = json.loads(envelope_line)
    # Full envelope carries subsystem_status.
    assert "subsystem_status" in envelope["data"]


# ---------------------------------------------------------------------------
# Feature flag parity
# ---------------------------------------------------------------------------


def test_doctor_same_text_output_under_v06_and_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain-text doctor output must be identical whether CODEPROBE_DEFAULTS is unset or v0.6."""
    runner = CliRunner()

    # Baseline — unset
    monkeypatch.delenv(CODEPROBE_DEFAULTS_ENV, raising=False)
    unset_result = runner.invoke(main, ["doctor"])

    # v0.6 — explicit opt-out
    monkeypatch.setenv(CODEPROBE_DEFAULTS_ENV, "v0.6")
    v06_result = runner.invoke(main, ["doctor"])

    assert unset_result.output == v06_result.output
    assert unset_result.exit_code == v06_result.exit_code


# ---------------------------------------------------------------------------
# mine: v0.7 auto-derives goal + narrative_source on a commit-only fixture.
# We run a light check without actually invoking the heavy mining pipeline.
# ---------------------------------------------------------------------------


def _git_init_with_commits(path: Path, commits: int = 2) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "t@t.t"], check=True
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "t"], check=True
    )
    for i in range(commits):
        (path / f"f{i}.txt").write_text(f"c{i}\n")
        subprocess.run(
            ["git", "-C", str(path), "add", "."], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(path), "commit", "-q", "-m", f"c{i}"],
            check=True,
            capture_output=True,
        )


def test_mine_v07_auto_derives_narrative_source_on_commit_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a commit-only fixture with v0.7 set, narrative_source should resolve to ('commits',)."""
    from codeprobe.config.defaults import (
        resolve_narrative_source,
        scan_repo_shape,
    )

    _git_init_with_commits(tmp_path, commits=3)
    shape = scan_repo_shape(tmp_path)
    assert shape.commit_count >= 3
    assert shape.has_merged_prs is False

    value, source = resolve_narrative_source(shape)
    assert value == ("commits",)
    assert source == "auto-detected"


def test_mine_v07_raises_narrative_undetectable_on_empty_fixture(
    tmp_path: Path,
) -> None:
    """Empty fixture → NARRATIVE_SOURCE_UNDETECTABLE with prescriptive next-try."""
    from codeprobe.config.defaults import (
        PrescriptiveError,
        resolve_narrative_source,
        scan_repo_shape,
    )

    shape = scan_repo_shape(tmp_path)
    with pytest.raises(PrescriptiveError) as exc_info:
        resolve_narrative_source(shape)
    assert exc_info.value.code == "NARRATIVE_SOURCE_UNDETECTABLE"
    assert exc_info.value.next_try_flag == "--narrative-source"
    assert exc_info.value.next_try_value == "commits"


# ---------------------------------------------------------------------------
# run: v0.7 resolves max_cost_usd / timeout defaults.
# Verified at the resolver layer — full run_eval requires an experiment.
# ---------------------------------------------------------------------------


def test_run_v07_max_cost_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CODEPROBE_DEFAULTS_ENV, "v0.7")
    from codeprobe.config.defaults import (
        resolve_max_cost_usd,
        use_v07_defaults,
    )

    assert use_v07_defaults() is True
    value, _ = resolve_max_cost_usd()
    assert value == 10.00


def test_run_v06_max_cost_default_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CODEPROBE_DEFAULTS_ENV, raising=False)
    from codeprobe.config.defaults import use_v07_defaults

    assert use_v07_defaults() is False
