"""Bare-invocation matrix harness — Phase A baseline snapshot.

Implements PRD §G7, §M1, and §T4. Runs the full
``codeprobe mine → run → interpret`` chain against a programmatic matrix
of synthetic local repos and records the outcome as a JSON baseline that
subsequent phases (notably Phase C: defaults audit) can diff against.

Fixtures are built at test-collection time (pytest ``tmp_path`` + real
``git init``) — no external clones, no network. ``codeprobe run``'s agent
call is redirected through an in-test :class:`FakeAdapter` via
``monkeypatch``, so the agent binary is never invoked.

Baseline regeneration
---------------------

The on-disk baseline lives at ``tests/cli/baseline_bare_invocation.json``
and is the Phase A pre-change snapshot. To regenerate it after an
intentional behavior change::

    CODEPROBE_REBASELINE=1 pytest tests/cli/test_bare_invocation_matrix.py -q

Then inspect the diff, confirm it is expected, and commit the updated
JSON file. Without the env var the harness asserts that the current
outcomes match the committed baseline (this is the regression guard).

T4 auto-detection gate
----------------------

A separate stress test measures the mis-prediction rate of mine's
narrative-source auto-selection against the ``auto-squash-pr-heavy`` and
``commits-only`` shapes. The gate asserts a mis-prediction rate
< 20% — ``pytest.xfail`` is permitted for the gate itself (not the
overall matrix) so the target can be tightened over time.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli import main
from tests.cli.fixtures.synthetic_repos import (
    make_airgapped_env,
    make_auto_squash_heavy,
    make_commits_only,
    make_issue_tracked,
    make_pr_rich,
    patch_adapter,
    write_minimal_experiment,
)

BASELINE_PATH = Path(__file__).parent / "baseline_bare_invocation.json"

# Matrix ordering matters for deterministic baseline output.
FIXTURE_NAMES = (
    "pr-rich",
    "commits-only",
    "auto-squash-pr-heavy",
    "issue-tracked",
    "airgapped",
)

# Shared results dict mutated across parametrized cases; the finalizer
# test writes or asserts this at the end.
_RESULTS: dict[str, dict[str, dict]] = {}


# -- helpers ------------------------------------------------------------------


def _last_json_line(output: str) -> dict | None:
    """Return the last JSON-parseable line in *output*, or ``None``."""
    for line in reversed([ln for ln in output.splitlines() if ln.strip()]):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def _summarize_envelope(payload: dict | None, exit_code: int) -> dict:
    """Reduce a full envelope to the stable subset recorded in the baseline.

    Strips variable fields (``version``, ``tasks_dir`` paths, warnings
    bodies) and keeps only deterministic signals: ``ok`` / ``exit_code``
    / ``command`` / error shape / a few command-specific counters.
    """
    if payload is None or payload.get("record_type") != "envelope":
        return {
            "ok": False,
            "exit_code": exit_code,
            "command": None,
            "error_code": "no_envelope",
            "error_kind": None,
            "error_terminal": None,
            "task_count": None,
            "has_results": None,
            "detected_narrative_source": None,
        }

    data = payload.get("data") or {}
    error = payload.get("error") or {}

    return {
        "ok": bool(payload.get("ok", False)),
        "exit_code": int(payload.get("exit_code", exit_code)),
        "command": payload.get("command"),
        "error_code": error.get("code") if isinstance(error, dict) else None,
        "error_kind": error.get("kind") if isinstance(error, dict) else None,
        "error_terminal": (
            bool(error.get("terminal"))
            if isinstance(error, dict) and "terminal" in error
            else None
        ),
        "task_count": data.get("task_count"),
        "has_results": data.get("has_results"),
        "detected_narrative_source": data.get("narrative_source")
        or data.get("detected_narrative_source"),
    }


def _build_fixture(name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Dispatch to the appropriate builder and return the repo root."""
    if name == "pr-rich":
        return make_pr_rich(tmp_path, n_prs=3)
    if name == "commits-only":
        return make_commits_only(tmp_path, n_commits=10)
    if name == "auto-squash-pr-heavy":
        # Reduced counts keep the total harness runtime well under 120s
        # while preserving the "empty-body squash stream + RFC batch"
        # narrative signal the T4 gate measures.
        return make_auto_squash_heavy(tmp_path, n_prs=40, n_rfcs=5)
    if name == "issue-tracked":
        return make_issue_tracked(tmp_path, n_commits=10)
    if name == "airgapped":
        return make_airgapped_env(tmp_path, monkeypatch)
    raise ValueError(f"Unknown fixture name: {name!r}")


def _invoke_mine(repo: Path) -> tuple[dict, int]:
    """Run ``codeprobe mine --json`` on *repo*. Return (summary, exit_code)."""
    result = CliRunner().invoke(
        main,
        ["mine", str(repo), "--no-interactive", "--no-llm", "--json"],
    )
    payload = _last_json_line(result.output)
    return _summarize_envelope(payload, result.exit_code), result.exit_code


def _invoke_run(repo: Path) -> tuple[dict, int]:
    """Run ``codeprobe run --json`` on *repo*. Return (summary, exit_code)."""
    result = CliRunner().invoke(
        main,
        ["run", str(repo), "--agent", "claude", "--json"],
    )
    payload = _last_json_line(result.output)
    return _summarize_envelope(payload, result.exit_code), result.exit_code


def _invoke_interpret(repo: Path) -> tuple[dict, int]:
    """Run ``codeprobe interpret --format json`` on *repo*."""
    result = CliRunner().invoke(
        main,
        ["interpret", str(repo), "--format", "json"],
    )
    # interpret uses --format json via the legacy flag; emits an envelope
    # when the --json flag wiring is present. Parse whichever JSON line
    # we find.
    payload = _last_json_line(result.output)
    return _summarize_envelope(payload, result.exit_code), result.exit_code


# -- parametrized matrix ------------------------------------------------------


@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_bare_invocation_matrix(
    fixture_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run mine → run → interpret for *fixture_name* and record outcomes.

    Does NOT assert success or failure — the purpose is to snapshot the
    pre-change behavior. The regression guard in
    :func:`test_zz_finalize_baseline` is what compares against the
    committed baseline.
    """
    patch_adapter(monkeypatch)
    # Reset the mine_cmd module-level _CURRENT_TASKS_DIR between
    # parametrizations so stale state from a prior fixture's successful
    # probe run doesn't bleed the next fixture's envelope ``task_count``.
    # This is safe because the mine CLI writes through the global on
    # every real mine; we're just zeroing it at the harness boundary.
    import codeprobe.cli.mine_cmd as _mine_module

    monkeypatch.setattr(_mine_module, "_CURRENT_TASKS_DIR", None)

    repo = _build_fixture(fixture_name, tmp_path, monkeypatch)
    write_minimal_experiment(repo)

    mine_summary, _ = _invoke_mine(repo)
    run_summary, _ = _invoke_run(repo)
    interpret_summary, _ = _invoke_interpret(repo)

    _RESULTS[fixture_name] = {
        "mine": mine_summary,
        "run": run_summary,
        "interpret": interpret_summary,
    }


# -- T4 stress gate -----------------------------------------------------------


def _expected_detection(fixture_name: str) -> set[str]:
    """What narrative-source should mine auto-select for *fixture_name*?

    Returns a set of acceptable outcomes. When mine raises the INV1
    UsageError we record ``"none"`` because the contract says
    auto-detection should have picked an adapter before failing.
    """
    if fixture_name == "auto-squash-pr-heavy":
        # RFCs are present and discoverable; "rfcs" (with or without
        # "commits") should be picked. An outright "pr" would be wrong
        # because there are no merged PRs via gh; bare UsageError counts
        # as mis-prediction.
        return {"rfcs", "commits+rfcs", "rfcs+commits", "commits"}
    if fixture_name == "commits-only":
        return {"commits"}
    return set()


@pytest.mark.xfail(
    os.environ.get("CODEPROBE_T4_GATE_STRICT") != "1",
    reason=(
        "T4 auto-detection gate is tracked separately; Phase A captures "
        "baseline mis-prediction rate. Set CODEPROBE_T4_GATE_STRICT=1 "
        "to enforce the <20% threshold."
    ),
    strict=False,
)
def test_t4_narrative_source_auto_detection_threshold() -> None:
    """Stress-test: at least one per narrative-source type, <20% miss rate.

    Reads ``_RESULTS`` populated by the parametrized matrix above. If
    run in isolation without the matrix, the assertion is vacuous —
    which is why this test is ordered after the matrix via its name.
    """
    stress_cases = ("auto-squash-pr-heavy", "commits-only")
    misses = 0
    total = 0
    for case in stress_cases:
        total += 1
        expected = _expected_detection(case)
        summary = _RESULTS.get(case, {}).get("mine", {})
        detected = summary.get("detected_narrative_source")
        # A prescriptive error raised by _resolve_narrative_source means
        # the auto-detector gave up and asked the user for help; that
        # counts as a miss against the auto-detection target.
        if detected is None or detected not in expected:
            misses += 1

    assert total > 0, "T4 gate must inspect at least one stress case"
    miss_rate = misses / total
    assert miss_rate < 0.20, (
        f"Narrative-source auto-detection mis-prediction rate "
        f"{miss_rate:.0%} exceeds the 20% gate (misses={misses}, "
        f"total={total})."
    )


# -- baseline finalizer -------------------------------------------------------


def test_zz_finalize_baseline() -> None:
    """Write the baseline (when rebaselining) or assert parity with disk.

    The leading ``zz_`` ensures pytest runs this after all
    ``test_bare_invocation_matrix`` parametrizations (tests run in file
    order by default when collected from a single module).
    """
    # Sanity: the matrix must have populated every expected fixture.
    missing = [name for name in FIXTURE_NAMES if name not in _RESULTS]
    assert not missing, (
        f"Matrix did not record results for: {missing}. "
        "Did an earlier parametrization crash?"
    )

    # Stable key order in the output.
    current = {
        name: _RESULTS[name]
        for name in FIXTURE_NAMES
    }

    if os.environ.get("CODEPROBE_REBASELINE") == "1":
        BASELINE_PATH.write_text(
            json.dumps(current, sort_keys=True, indent=2) + "\n"
        )
        pytest.skip(
            f"Rebaselined {BASELINE_PATH.name}. Inspect the diff and "
            "commit the update."
        )

    if not BASELINE_PATH.exists():
        pytest.skip(
            f"No baseline at {BASELINE_PATH.name} — run with "
            "CODEPROBE_REBASELINE=1 to create it."
        )

    committed = json.loads(BASELINE_PATH.read_text())
    assert current == committed, (
        "Bare-invocation matrix outcomes diverged from the committed "
        "Phase A baseline. Regenerate deliberately with "
        "CODEPROBE_REBASELINE=1 only after confirming the behavior "
        "change is intended.\n\n"
        f"diff (current vs committed):\n"
        f"  current:   {json.dumps(current, sort_keys=True)}\n"
        f"  committed: {json.dumps(committed, sort_keys=True)}"
    )
