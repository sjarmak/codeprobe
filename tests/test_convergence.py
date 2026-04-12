"""Tests for the acceptance loop convergence controller.

Covers every acceptance criterion from the work-unit spec:

- Construction smoke test (``from acceptance.converge import ...``)
- Monotonic progress → CONTINUE
- Regression (pass count decreases) → HALT_REGRESSION with escalation report
- Oscillation (>2 flips) → quarantined
- Three-strike identical evidence → ESCALATE with report mention
- Max iterations → HALT_MAX_ITERATIONS
- Two consecutive all_pass → RELEASE
- Critical quarantined criterion blocks release even when all_pass twice
- Crash-resume: state persists across controller instances
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from acceptance.converge import (
    BLOCKING_SEVERITIES,
    DEFAULT_MAX_ITERATIONS,
    ConvergenceController,
    Decision,
    DecisionResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _verdict(
    iteration: int,
    *,
    pass_count: int = 0,
    fail_count: int = 0,
    skip_count: int = 0,
    all_pass: bool = False,
    failures: list[dict[str, Any]] | None = None,
    quarantined: list[str] | None = None,
    status: str = "EVALUATED",
    evaluated_pct: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Build a verdict dict matching the spec shape."""
    return {
        "iteration": iteration,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "skip_count": skip_count,
        "evaluated_pct": evaluated_pct
        or {"structural": 100, "behavioral": 100, "statistical": 0},
        "all_pass": all_pass,
        "status": status,
        "failures": failures or [],
        "quarantined": quarantined or [],
    }


def _fail(
    criterion_id: str,
    evidence: str = "exit code 1: generic failure",
    severity: str = "medium",
) -> dict[str, Any]:
    return {
        "criterion_id": criterion_id,
        "evidence": evidence,
        "severity": severity,
    }


# ---------------------------------------------------------------------------
# Construction / smoke
# ---------------------------------------------------------------------------


def test_construction_smoke(tmp_path: Path) -> None:
    """Controller must be constructible from a plain directory path."""
    cc = ConvergenceController(str(tmp_path / "converge-state"))
    assert cc.decide().decision == Decision.CONTINUE
    assert cc.get_quarantined() == []
    assert cc.is_release_ready() is False


def test_construction_accepts_existing_dir(tmp_path: Path) -> None:
    """An existing directory should be treated as a DB parent directory."""
    cc = ConvergenceController(tmp_path)
    cc.record_verdict(_verdict(1, pass_count=5, fail_count=0, all_pass=True))
    # converge.db should live under tmp_path
    assert (tmp_path / "converge.db").exists()


def test_rejects_bad_max_iterations(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ConvergenceController(tmp_path / "db", max_iterations=0)


def test_rejects_bad_verdict(tmp_path: Path) -> None:
    cc = ConvergenceController(tmp_path / "db")
    with pytest.raises(ValueError):
        cc.record_verdict({"iteration": 1})  # missing keys


# ---------------------------------------------------------------------------
# Monotonic progress
# ---------------------------------------------------------------------------


def test_monotonic_progress_continues(tmp_path: Path) -> None:
    cc = ConvergenceController(tmp_path / "db")
    cc.record_verdict(
        _verdict(
            1,
            pass_count=10,
            fail_count=5,
            failures=[_fail("A", evidence="e1"), _fail("B", evidence="e2")],
        )
    )
    cc.record_verdict(
        _verdict(2, pass_count=12, fail_count=3, failures=[_fail("A", evidence="e1b")])
    )
    cc.record_verdict(
        _verdict(3, pass_count=14, fail_count=1, failures=[_fail("C", evidence="e3")])
    )

    result = cc.decide()
    assert result.decision == Decision.CONTINUE


# ---------------------------------------------------------------------------
# Regression
# ---------------------------------------------------------------------------


def test_regression_halts(tmp_path: Path) -> None:
    """Decreasing pass count triggers HALT_REGRESSION with report."""
    cc = ConvergenceController(tmp_path / "db")
    cc.record_verdict(_verdict(1, pass_count=18, fail_count=7, failures=[_fail("X")]))
    cc.record_verdict(_verdict(2, pass_count=20, fail_count=5, failures=[_fail("X")]))
    cc.record_verdict(_verdict(3, pass_count=15, fail_count=10, failures=[_fail("X")]))

    result = cc.decide()
    assert result.decision == Decision.HALT_REGRESSION
    assert result.context["previous"] == 20
    assert result.context["current"] == 15
    assert result.context["iteration"] == 3

    report = cc.get_escalation_report()
    assert "Regression" in report
    assert "20" in report and "15" in report


# ---------------------------------------------------------------------------
# Oscillation / quarantine
# ---------------------------------------------------------------------------


def test_oscillating_criterion_is_quarantined(tmp_path: Path) -> None:
    """A criterion that flips fail/pass/fail/pass gets quarantined (>2 flips)."""
    cc = ConvergenceController(tmp_path / "db")
    # fail → pass → fail → pass → fail = 4 flips (exceeds OSCILLATION_FLIP_LIMIT=2)
    cc.record_verdict(
        _verdict(
            1, pass_count=5, fail_count=1, failures=[_fail("FLAKE", severity="medium")]
        )
    )
    cc.record_verdict(_verdict(2, pass_count=6, fail_count=0, failures=[]))
    cc.record_verdict(
        _verdict(
            3, pass_count=5, fail_count=1, failures=[_fail("FLAKE", severity="medium")]
        )
    )
    cc.record_verdict(_verdict(4, pass_count=6, fail_count=0, failures=[]))
    cc.record_verdict(
        _verdict(
            5, pass_count=5, fail_count=1, failures=[_fail("FLAKE", severity="medium")]
        )
    )

    quarantined = cc.get_quarantined()
    assert "FLAKE" in quarantined

    report = cc.get_escalation_report()
    assert "FLAKE" in report
    assert "Quarantined" in report or "quarantined" in report.lower()


# ---------------------------------------------------------------------------
# Three-strike rule
# ---------------------------------------------------------------------------


def test_three_strike_same_evidence_escalates(tmp_path: Path) -> None:
    cc = ConvergenceController(tmp_path / "db")
    evidence = "exit code 1: No experiment found"
    for i in range(1, 4):
        cc.record_verdict(
            _verdict(
                i,
                pass_count=18,
                fail_count=1,
                failures=[_fail("MINE-001", evidence=evidence, severity="critical")],
            )
        )

    result = cc.decide()
    assert result.decision == Decision.ESCALATE
    stuck_ids = [s["criterion_id"] for s in result.context["stuck"]]
    assert "MINE-001" in stuck_ids

    report = cc.get_escalation_report()
    assert "MINE-001" in report
    assert "Three-Strike" in report or "three-strike" in report.lower()


def test_three_strike_requires_identical_evidence(tmp_path: Path) -> None:
    """Same criterion with different evidence each iteration does NOT trigger three-strike."""
    cc = ConvergenceController(tmp_path / "db")
    for i, ev in enumerate(["error A", "error B", "error C"], start=1):
        cc.record_verdict(
            _verdict(
                i, pass_count=10, fail_count=1, failures=[_fail("DRIFT", evidence=ev)]
            )
        )

    # Not stuck (evidence keeps changing), progress is flat — should CONTINUE not ESCALATE.
    result = cc.decide()
    assert result.decision != Decision.ESCALATE


# ---------------------------------------------------------------------------
# Max iterations
# ---------------------------------------------------------------------------


def test_max_iterations_halts(tmp_path: Path) -> None:
    cc = ConvergenceController(tmp_path / "db", max_iterations=DEFAULT_MAX_ITERATIONS)
    # Record 10 verdicts with non-monotonic identical pass counts and rotating
    # failures so regression and three-strike do not trigger first.
    for i in range(1, DEFAULT_MAX_ITERATIONS + 1):
        cc.record_verdict(
            _verdict(
                i,
                pass_count=10,
                fail_count=1,
                failures=[_fail(f"ROT-{i}", evidence=f"err {i}")],
            )
        )

    result = cc.decide()
    assert result.decision == Decision.HALT_MAX_ITERATIONS
    assert result.context["cap"] == DEFAULT_MAX_ITERATIONS


def test_custom_max_iterations(tmp_path: Path) -> None:
    cc = ConvergenceController(tmp_path / "db", max_iterations=3)
    for i in range(1, 4):
        cc.record_verdict(
            _verdict(
                i,
                pass_count=5,
                fail_count=1,
                failures=[_fail(f"X-{i}", evidence=f"err {i}")],
            )
        )
    assert cc.decide().decision == Decision.HALT_MAX_ITERATIONS


# ---------------------------------------------------------------------------
# Release readiness
# ---------------------------------------------------------------------------


def test_release_ready_two_consecutive_all_pass(tmp_path: Path) -> None:
    cc = ConvergenceController(tmp_path / "db")
    cc.record_verdict(_verdict(1, pass_count=25, fail_count=0, all_pass=True))
    cc.record_verdict(_verdict(2, pass_count=25, fail_count=0, all_pass=True))

    assert cc.is_release_ready() is True
    result = cc.decide()
    assert result.decision == Decision.RELEASE


def test_release_requires_two_consecutive(tmp_path: Path) -> None:
    cc = ConvergenceController(tmp_path / "db")
    cc.record_verdict(_verdict(1, pass_count=10, fail_count=1, failures=[_fail("A")]))
    cc.record_verdict(_verdict(2, pass_count=25, fail_count=0, all_pass=True))

    assert cc.is_release_ready() is False
    assert cc.decide().decision != Decision.RELEASE


def test_critical_quarantined_blocks_release(tmp_path: Path) -> None:
    """Even with two all_pass verdicts, a critical quarantined criterion blocks release."""
    cc = ConvergenceController(tmp_path / "db")
    # Create a critical criterion that oscillates across 5 iterations.
    cc.record_verdict(
        _verdict(
            1,
            pass_count=5,
            fail_count=1,
            failures=[_fail("SEC-1", severity="critical")],
        )
    )
    cc.record_verdict(_verdict(2, pass_count=6, fail_count=0, failures=[]))
    cc.record_verdict(
        _verdict(
            3,
            pass_count=5,
            fail_count=1,
            failures=[_fail("SEC-1", severity="critical")],
        )
    )
    cc.record_verdict(_verdict(4, pass_count=6, fail_count=0, failures=[]))
    cc.record_verdict(
        _verdict(
            5,
            pass_count=5,
            fail_count=1,
            failures=[_fail("SEC-1", severity="critical")],
        )
    )

    # Now record two all_pass verdicts on top.
    cc.record_verdict(_verdict(6, pass_count=25, fail_count=0, all_pass=True))
    cc.record_verdict(_verdict(7, pass_count=25, fail_count=0, all_pass=True))

    assert "SEC-1" in cc.get_quarantined()
    assert cc.is_release_ready() is False
    assert cc.decide().decision != Decision.RELEASE

    report = cc.get_escalation_report()
    assert "SEC-1" in report
    assert "blocks release" in report


def test_medium_quarantined_does_not_block_release(tmp_path: Path) -> None:
    cc = ConvergenceController(tmp_path / "db")
    # Oscillate a medium criterion so it gets quarantined.
    cc.record_verdict(
        _verdict(
            1, pass_count=5, fail_count=1, failures=[_fail("FLAKY", severity="medium")]
        )
    )
    cc.record_verdict(_verdict(2, pass_count=6, fail_count=0, failures=[]))
    cc.record_verdict(
        _verdict(
            3, pass_count=5, fail_count=1, failures=[_fail("FLAKY", severity="medium")]
        )
    )
    cc.record_verdict(_verdict(4, pass_count=6, fail_count=0, failures=[]))
    cc.record_verdict(
        _verdict(
            5, pass_count=5, fail_count=1, failures=[_fail("FLAKY", severity="medium")]
        )
    )
    cc.record_verdict(_verdict(6, pass_count=25, fail_count=0, all_pass=True))
    cc.record_verdict(_verdict(7, pass_count=25, fail_count=0, all_pass=True))

    assert "FLAKY" in cc.get_quarantined()
    assert cc.is_release_ready() is True
    assert cc.decide().decision == Decision.RELEASE


# ---------------------------------------------------------------------------
# Crash-resume persistence
# ---------------------------------------------------------------------------


def test_state_persists_across_controller_instances(tmp_path: Path) -> None:
    """Kill the controller mid-run; a new instance pointed at the same path resumes."""
    db_dir = tmp_path / "state"
    cc1 = ConvergenceController(db_dir)
    cc1.record_verdict(_verdict(1, pass_count=10, fail_count=2, failures=[_fail("A")]))
    cc1.record_verdict(_verdict(2, pass_count=12, fail_count=1, failures=[_fail("A")]))
    del cc1

    cc2 = ConvergenceController(db_dir)
    # History intact: decide() sees both iterations and reports CONTINUE (monotonic).
    result = cc2.decide()
    assert result.decision == Decision.CONTINUE
    assert result.context.get("iterations") == 2

    # Append a third verdict that triggers regression — previous state carried over.
    cc2.record_verdict(_verdict(3, pass_count=5, fail_count=8, failures=[_fail("A")]))
    result = cc2.decide()
    assert result.decision == Decision.HALT_REGRESSION
    assert result.context["previous"] == 12


def test_quarantine_persists_across_instances(tmp_path: Path) -> None:
    db_dir = tmp_path / "state"
    cc1 = ConvergenceController(db_dir)
    for i, failed in enumerate([True, False, True, False, True], start=1):
        cc1.record_verdict(
            _verdict(
                i,
                pass_count=5 if failed else 6,
                fail_count=1 if failed else 0,
                failures=[_fail("OSC", severity="high")] if failed else [],
            )
        )
    assert "OSC" in cc1.get_quarantined()

    cc2 = ConvergenceController(db_dir)
    assert "OSC" in cc2.get_quarantined()


# ---------------------------------------------------------------------------
# Misc / contract
# ---------------------------------------------------------------------------


def test_blocking_severities_constant_is_frozen() -> None:
    assert BLOCKING_SEVERITIES == frozenset({"critical", "high"})


def test_decision_result_is_frozen(tmp_path: Path) -> None:
    cc = ConvergenceController(tmp_path / "db")
    result: DecisionResult = cc.decide()
    with pytest.raises(Exception):
        result.decision = Decision.RELEASE  # type: ignore[misc]


def test_empty_history_continues(tmp_path: Path) -> None:
    cc = ConvergenceController(tmp_path / "db")
    result = cc.decide()
    assert result.decision == Decision.CONTINUE
    assert "no verdicts" in result.reason.lower()


def test_record_verdict_replaces_same_iteration(tmp_path: Path) -> None:
    """Re-recording the same iteration replaces the prior value (idempotent)."""
    cc = ConvergenceController(tmp_path / "db")
    cc.record_verdict(_verdict(1, pass_count=10, fail_count=2, failures=[_fail("A")]))
    cc.record_verdict(_verdict(1, pass_count=12, fail_count=0, all_pass=True))
    cc.record_verdict(_verdict(2, pass_count=12, fail_count=0, all_pass=True))
    assert cc.is_release_ready() is True
