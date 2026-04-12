"""Deterministic convergence controller for the acceptance loop.

Tracks verdict history across iterations and makes continue / halt / release
/ escalate decisions. No LLM calls — this is pure policy enforcement over
structured verdict JSON produced upstream by the evaluator.

Responsibilities:

- Persist verdicts in SQLite (WAL mode) so a crashed runner can resume.
- Detect monotonic-progress regressions (pass_count must not decrease).
- Detect oscillating criteria (pass/fail flips more than twice) and
  quarantine them.
- Detect the three-strike rule: same criterion failing three iterations in a
  row with the same evidence escalates for human intervention.
- Gate release on two consecutive ``all_pass`` verdicts AND absence of any
  blocking (critical/high) quarantined criteria.
- Produce a markdown escalation report summarizing stuck / quarantined
  criteria so humans know what to look at.

This module is ZFC-compliant: all decisions are deterministic arithmetic /
state comparisons, not semantic judgments.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

DEFAULT_MAX_ITERATIONS = 10
"""Default iteration cap before HALT_MAX_ITERATIONS fires."""

BLOCKING_SEVERITIES: frozenset[str] = frozenset({"critical", "high"})
"""Severities that still block release even when the criterion is quarantined."""

OSCILLATION_FLIP_LIMIT = 2
"""A criterion that flips pass/fail more than this many times is quarantined."""

THREE_STRIKE_WINDOW = 3
"""Number of consecutive identical failures before ESCALATE fires."""

_REQUIRED_VERDICT_KEYS: frozenset[str] = frozenset(
    {"iteration", "pass_count", "fail_count", "all_pass", "failures"}
)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class Decision(Enum):
    """Terminal decision returned by :meth:`ConvergenceController.decide`."""

    CONTINUE = "continue"
    HALT_MAX_ITERATIONS = "halt_max_iterations"
    HALT_REGRESSION = "halt_regression"
    HALT_STUCK = "halt_stuck"
    RELEASE = "release"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class DecisionResult:
    """Result of a :meth:`ConvergenceController.decide` call.

    Attributes
    ----------
    decision:
        The enum decision.
    reason:
        Human-readable explanation.
    context:
        Optional structured context (criterion ids, regression delta, etc.).
    """

    decision: Decision
    reason: str
    context: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS verdicts (
    iteration  INTEGER PRIMARY KEY,
    data       TEXT    NOT NULL,
    created_at REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS quarantined (
    criterion_id TEXT PRIMARY KEY,
    reason       TEXT NOT NULL,
    severity     TEXT NOT NULL,
    first_iter   INTEGER NOT NULL
);
"""


class ConvergenceController:
    """Stateful controller that persists verdict history in SQLite.

    Parameters
    ----------
    db_path:
        Either a directory (in which case ``converge.db`` is created inside)
        or a file path that will be used directly as the SQLite database.
    max_iterations:
        Iteration cap before the controller halts with
        ``Decision.HALT_MAX_ITERATIONS``. Defaults to
        :data:`DEFAULT_MAX_ITERATIONS`.
    """

    def __init__(
        self,
        db_path: str | Path,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
    ) -> None:
        if max_iterations < 1:
            raise ValueError(f"max_iterations must be >= 1, got {max_iterations!r}")

        resolved = Path(db_path)
        if resolved.exists() and resolved.is_dir():
            resolved = resolved / "converge.db"
        elif not resolved.suffix:
            # Treat extension-less paths as directories to be created.
            resolved.mkdir(parents=True, exist_ok=True)
            resolved = resolved / "converge.db"
        else:
            resolved.parent.mkdir(parents=True, exist_ok=True)

        self._db_path: Path = resolved
        self._max_iterations: int = max_iterations
        self._init_db()

    # ------------------------------------------------------------------ db

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------ api

    def record_verdict(self, verdict: dict[str, Any]) -> None:
        """Store a verdict and refresh derived quarantine state.

        Validates that the required keys are present and that ``iteration`` is
        a positive integer. Replaces any existing row for the same iteration
        so re-runs of the same iteration are idempotent.
        """
        self._validate_verdict(verdict)
        data = json.dumps(verdict, sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO verdicts (iteration, data, created_at) VALUES (?, ?, ?)",
                (int(verdict["iteration"]), data, time.time()),
            )

        # Refresh oscillation-based quarantine after every new verdict so
        # get_quarantined() reflects the latest history.
        self._refresh_oscillation_quarantine()

    def decide(self) -> DecisionResult:
        """Apply policy rules in priority order and return the decision."""
        history = self._load_history()

        if not history:
            return DecisionResult(
                Decision.CONTINUE,
                reason="no verdicts recorded yet",
                context={},
            )

        # 1. Release-ready wins — two consecutive all_pass with no blocking
        #    quarantined criteria. all_pass=True implies fail_count=0 so
        #    regression cannot coexist with release readiness.
        if self.is_release_ready():
            return DecisionResult(
                Decision.RELEASE,
                reason="two consecutive all_pass verdicts with no blocking quarantined criteria",
                context={"last_iteration": history[-1]["iteration"]},
            )

        # 2. Regression — pass_count dropped between consecutive verdicts.
        #    Checked before three-strike because a regression means the most
        #    recent fix actively made things worse, which is more urgent than
        #    "stuck on the same failure".
        regression = self._detect_regression(history)
        if regression is not None:
            return DecisionResult(
                Decision.HALT_REGRESSION,
                reason=(
                    f"pass_count regressed from {regression['previous']} "
                    f"to {regression['current']} at iteration {regression['iteration']}"
                ),
                context=regression,
            )

        # 3. Three-strike rule — same criterion failing 3x with identical
        #    evidence. Human intervention required.
        stuck = self._detect_three_strike(history)
        if stuck:
            return DecisionResult(
                Decision.ESCALATE,
                reason=f"three-strike rule triggered for {len(stuck)} criterion(ia)",
                context={"stuck": stuck},
            )

        # 4. Max iterations cap.
        if len(history) >= self._max_iterations:
            return DecisionResult(
                Decision.HALT_MAX_ITERATIONS,
                reason=f"iteration cap reached ({self._max_iterations})",
                context={"iterations": len(history), "cap": self._max_iterations},
            )

        # 5. Default — still making progress, continue.
        return DecisionResult(
            Decision.CONTINUE,
            reason="monotonic progress maintained, fixable failures remain",
            context={"iterations": len(history)},
        )

    def get_quarantined(self) -> list[str]:
        """Return quarantined criterion IDs ordered by first-seen iteration."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT criterion_id FROM quarantined ORDER BY first_iter ASC, criterion_id ASC"
            ).fetchall()
        return [row["criterion_id"] for row in rows]

    def get_escalation_report(self) -> str:
        """Render a markdown escalation report summarizing the current state."""
        history = self._load_history()
        lines: list[str] = ["# Convergence Escalation Report", ""]

        if not history:
            lines.append("_No verdicts recorded yet._")
            return "\n".join(lines)

        latest = history[-1]
        lines.extend(
            [
                f"- Iterations recorded: **{len(history)}**",
                f"- Latest iteration: **{latest['iteration']}**",
                f"- Latest pass / fail / skip: "
                f"**{latest.get('pass_count', 0)} / "
                f"{latest.get('fail_count', 0)} / "
                f"{latest.get('skip_count', 0)}**",
                f"- all_pass: **{latest.get('all_pass', False)}**",
                "",
            ]
        )

        regression = self._detect_regression(history)
        if regression is not None:
            lines.extend(
                [
                    "## Regression",
                    (
                        f"Pass count regressed from **{regression['previous']}** to "
                        f"**{regression['current']}** at iteration "
                        f"**{regression['iteration']}**."
                    ),
                    "",
                ]
            )

        stuck = self._detect_three_strike(history)
        if stuck:
            lines.append("## Three-Strike (stuck) criteria")
            for entry in stuck:
                lines.append(
                    f"- `{entry['criterion_id']}` "
                    f"(severity: {entry['severity']}, "
                    f"failed {entry['consecutive']} iterations in a row)"
                )
                lines.append(f"  - Evidence: {entry['evidence']}")
            lines.append("")

        quarantined = self._get_quarantined_rows()
        if quarantined:
            lines.append("## Quarantined criteria (oscillating)")
            for row in quarantined:
                blocking = row["severity"] in BLOCKING_SEVERITIES
                tag = " **(blocks release)**" if blocking else ""
                lines.append(
                    f"- `{row['criterion_id']}` "
                    f"(severity: {row['severity']}, first seen iter {row['first_iter']}){tag}"
                )
                lines.append(f"  - Reason: {row['reason']}")
            lines.append("")

        if not regression and not stuck and not quarantined:
            lines.append("_No escalations — controller is making monotonic progress._")

        return "\n".join(lines).rstrip() + "\n"

    def is_release_ready(self) -> bool:
        """Return True iff the last two verdicts are all_pass AND no blocking quarantine."""
        history = self._load_history()
        if len(history) < 2:
            return False

        last, prev = history[-1], history[-2]
        if not (bool(last.get("all_pass")) and bool(prev.get("all_pass"))):
            return False

        if self._blocking_quarantined():
            return False

        return True

    # -------------------------------------------------------------- internals

    def _validate_verdict(self, verdict: dict[str, Any]) -> None:
        if not isinstance(verdict, dict):
            raise TypeError(f"verdict must be dict, got {type(verdict).__name__}")
        missing = _REQUIRED_VERDICT_KEYS - verdict.keys()
        if missing:
            raise ValueError(f"verdict missing required keys: {sorted(missing)}")
        iteration = verdict["iteration"]
        if not isinstance(iteration, int) or iteration < 0:
            raise ValueError(f"iteration must be non-negative int, got {iteration!r}")
        failures = verdict.get("failures") or []
        if not isinstance(failures, list):
            raise ValueError("failures must be a list")
        for failure in failures:
            if not isinstance(failure, dict):
                raise ValueError(f"failure must be dict, got {type(failure).__name__}")
            if "criterion_id" not in failure:
                raise ValueError("failure missing criterion_id")

    def _load_history(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT data FROM verdicts ORDER BY iteration ASC"
            ).fetchall()
        return [json.loads(row["data"]) for row in rows]

    def _detect_regression(
        self, history: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Return regression info if pass_count ever decreased, else None."""
        for prev, curr in zip(history, history[1:]):
            prev_pass = int(prev.get("pass_count", 0))
            curr_pass = int(curr.get("pass_count", 0))
            if curr_pass < prev_pass:
                return {
                    "iteration": curr.get("iteration"),
                    "previous": prev_pass,
                    "current": curr_pass,
                    "delta": curr_pass - prev_pass,
                }
        return None

    def _detect_three_strike(
        self, history: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Find criteria failing THREE_STRIKE_WINDOW iterations in a row with identical evidence."""
        if len(history) < THREE_STRIKE_WINDOW:
            return []

        window = history[-THREE_STRIKE_WINDOW:]
        # Map criterion_id -> list of (evidence, severity) across the window.
        per_crit: dict[str, list[tuple[str, str]]] = {}
        for verdict in window:
            seen_in_verdict: set[str] = set()
            for failure in verdict.get("failures") or []:
                cid = failure.get("criterion_id")
                if cid is None or cid in seen_in_verdict:
                    continue
                seen_in_verdict.add(cid)
                per_crit.setdefault(cid, []).append(
                    (
                        str(failure.get("evidence", "")),
                        str(failure.get("severity", "medium")),
                    )
                )

        stuck: list[dict[str, Any]] = []
        for cid, entries in per_crit.items():
            if len(entries) < THREE_STRIKE_WINDOW:
                continue
            evidence_set = {ev for ev, _sev in entries}
            if len(evidence_set) != 1:
                continue  # evidence changed — not a strict stuck case
            evidence = next(iter(evidence_set))
            severity = entries[-1][1]
            stuck.append(
                {
                    "criterion_id": cid,
                    "evidence": evidence,
                    "severity": severity,
                    "consecutive": len(entries),
                }
            )
        stuck.sort(key=lambda s: s["criterion_id"])
        return stuck

    def _refresh_oscillation_quarantine(self) -> None:
        """Walk history and quarantine any criterion that flipped > OSCILLATION_FLIP_LIMIT times."""
        history = self._load_history()
        if len(history) < 2:
            return

        # Collect all criterion IDs ever seen (first appearance iteration too).
        first_seen: dict[str, int] = {}
        last_severity: dict[str, str] = {}
        for verdict in history:
            for failure in verdict.get("failures") or []:
                cid = failure.get("criterion_id")
                if cid is None:
                    continue
                first_seen.setdefault(cid, int(verdict.get("iteration", 0)))
                last_severity[cid] = str(failure.get("severity", "medium"))

        quarantine_now: list[tuple[str, str, str, int]] = []
        for cid, first_iter in first_seen.items():
            timeline: list[bool] = []  # True = fail, False = pass
            for verdict in history:
                if int(verdict.get("iteration", -1)) < first_iter:
                    continue
                failed = any(
                    f.get("criterion_id") == cid
                    for f in (verdict.get("failures") or [])
                )
                timeline.append(failed)
            if len(timeline) < 2:
                continue
            flips = sum(1 for a, b in zip(timeline, timeline[1:]) if a != b)
            if flips > OSCILLATION_FLIP_LIMIT:
                quarantine_now.append(
                    (
                        cid,
                        f"oscillated {flips} times across {len(timeline)} iterations",
                        last_severity.get(cid, "medium"),
                        first_iter,
                    )
                )

        if not quarantine_now:
            return

        with self._connect() as conn:
            for cid, reason, severity, first_iter in quarantine_now:
                conn.execute(
                    """
                    INSERT INTO quarantined (criterion_id, reason, severity, first_iter)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(criterion_id) DO UPDATE SET
                        reason   = excluded.reason,
                        severity = excluded.severity
                    """,
                    (cid, reason, severity, first_iter),
                )

    def _get_quarantined_rows(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT criterion_id, reason, severity, first_iter "
                "FROM quarantined ORDER BY first_iter ASC, criterion_id ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def _blocking_quarantined(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT criterion_id FROM quarantined WHERE severity IN (?, ?)",
                ("critical", "high"),
            ).fetchall()
        return [row["criterion_id"] for row in rows]
