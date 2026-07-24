"""Infra-failure triage — structural classification of trial outcomes.

A trial that crashed on infrastructure (output-token-ceiling overrun,
quota/OAuth exhaustion, rate limit, network/timeout, MCP connect failure, ...)
is stamped ``automated_score=0.0`` by the executor, but that 0.0 is an
unrecoverable infrastructure casualty, not a measurement of solution quality.
Rolling it into the reward mean biases a comparison toward zero, and a run that
still holds unresolved infra casualties is not safely quotable (the 9tk
flagship confirm run lost a ``with-sg-full`` trial to the 32000-output-token
CLI ceiling, dragging that arm's mean 0.72 -> 0.645; codeprobe-77z).

This module classifies each :class:`CompletedTask` from STRUCTURAL and STRING
signals only — the trial ``status``, whether ``scoring_details`` ran
end-to-end, the adapter-declared ``result_subtype``, the executor's
``error_category``, and infra fault markers in the recorded error text. It
performs NO semantic scoring and applies NO score threshold: a genuinely low
score is a real data point and is never reclassified as infra here (ZFC-clean —
string-signal fault classification, the same shape as the executor's own
``_classify_error``).

Generalizes the quota-only exclusion of codeprobe-a8r
(:func:`codeprobe.analysis.stats.is_quota_casualty`, keyed on
``error_category == 'quota'``) to the full infra-failure family. A quota
casualty is one species of infra failure, so :func:`is_infra_failure` is a
strict superset of ``is_quota_casualty``.

Imports only :mod:`codeprobe.models` on purpose: ``analysis.stats`` imports
this module to build the reward population, so a dependency back on ``stats``
would close an import cycle.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from codeprobe.models.experiment import CompletedTask


class TrialClass(str, Enum):
    """Structural outcome class of a single trial."""

    # Scoring ran end-to-end — a real measurement, kept in the reward
    # population regardless of how low the score is.
    VALID = "valid"
    # Infrastructure casualty — excluded from the reward population and fails
    # the validity gate until re-run.
    INFRA_FAILURE = "infra_failure"
    # Terminal agent failure that is NOT infra (e.g. error_max_turns) — a
    # genuine 0.0 measurement. Kept distinct from infra so cap-contamination
    # (codeprobe-8up) is not conflated with a crash, and so the gate never asks
    # for a re-run that would burn budget hitting the same cap.
    GENUINE_FAILURE = "genuine_failure"


# ``result_subtype`` values that mark an adapter-declared TERMINAL agent
# failure: the agent ran to a protocol stop condition and its 0.0 is a real
# measurement. Mirrors ``adapters.claude._TERMINAL_RESULT_SUBTYPES`` — the
# executor projects ``AgentOutput.error_terminal`` onto ``status='failed'``
# (executor.py:828), and this is the CompletedTask-level proxy for the same
# signal on rows whose adapter did not set it.
_TERMINAL_RESULT_SUBTYPES: frozenset[str] = frozenset({"error_max_turns"})

# ``error_category`` values the executor assigns to non-agent faults: 'quota'
# (OAuth-limit detection, codeprobe-9xrl), 'timeout' (subprocess wall-clock),
# and 'system' (OSError / MemoryError) are infrastructure, never solution
# quality. 'agent' is the ambiguous fallback — the agent process itself
# failed — so it is decided by the error-text signature instead.
_INFRA_ERROR_CATEGORIES: frozenset[str] = frozenset({"quota", "timeout", "system"})

# Word-anchored fault signatures matched against the recorded error text.
# Mechanical markers emitted by the CLI/runtime — NOT semantic quality signals.
# Anchored with ``\b`` so vocabulary that merely CONTAINS a marker ("the rate
# limiter tests failed", "OpenAPI errors") does not drag a genuine terminal
# failure out of the reward population.
_INFRA_TEXT_PATTERNS: tuple[str, ...] = (
    # The codeprobe-9tk / 0d4ec3ad crash: NNNN is the CLI's per-response ceiling.
    r"exceeded the \d+ output token maximum",
    r"\bapi error\b",
    r"\brate[ _-]?limit(?:ed|s)?\b",
    r"\busage limit\b",
    r"\bsession limit\b",
    r"\bquota (?:exceeded|exhausted)\b",
    r"\boauth\b",
    r"\bbudget exceeded\b",
    r"\bconnection (?:refused|reset|error|failed|timed out)\b",
    r"\bnetwork is unreachable\b",
    r"\bfailed to connect\b",
    r"\btimed out\b",
)

_INFRA_TEXT_RE = re.compile("|".join(_INFRA_TEXT_PATTERNS), re.IGNORECASE)


def _error_text(task: CompletedTask) -> str:
    """Return the trial's recorded fault text.

    The executor stores the sanitized failure message under
    ``metadata['error']`` for every error/failed row (see ``_error_result`` and
    the adapter-error path in ``core/executor.py``). ``error_category`` is
    matched separately against :data:`_INFRA_ERROR_CATEGORIES`, so it is not
    folded in here.
    """
    metadata = task.metadata
    if not isinstance(metadata, dict):
        return ""
    err = metadata.get("error")
    return err if isinstance(err, str) else ""


def _matches_infra_text(task: CompletedTask) -> bool:
    """True when the trial's recorded fault text carries an infra signature."""
    text = _error_text(task)
    return bool(text) and _INFRA_TEXT_RE.search(text) is not None


def classify_trial(task: CompletedTask) -> TrialClass:
    """Classify a trial as VALID, INFRA_FAILURE, or GENUINE_FAILURE.

    Structural only — see the module docstring. The precedence is:

    1. An infra ``error_category`` (``quota`` / ``timeout`` / ``system``) or
       ``verdict == 'verifier_error'`` → INFRA_FAILURE. These are typed harness
       fault signals, so they outrank every other signal. Keeping them first is
       what makes ``is_infra_failure`` a strict superset of
       ``is_quota_casualty`` — including executor-stamped quota rows whose
       ``status`` still reads ``completed`` (codeprobe-9jxx) — and prevents a
       completed scoring attempt with a broken verifier from becoming a real
       0.0 measurement.
    2. Scoring ran end-to-end (``status == 'completed'`` or non-empty
       ``scoring_details``) → VALID, regardless of how low the score is.
    3. An adapter-declared terminal ``result_subtype``, or the executor's
       ``status == 'failed'`` projection of ``error_terminal`` → GENUINE_FAILURE.
       The agent got its turns; the 0.0 is a real measurement and re-running it
       would only hit the same cap.
    4. An infra marker in the recorded error text → INFRA_FAILURE. The weakest
       signal, so it ranks last among the positive checks: ``metadata['error']``
       is the adapter's verbatim ``envelope['result']`` (see
       ``adapters/telemetry.py:_extract_envelope_error``), a free-text field the
       evaluated agent can influence. Ranking it BELOW the structural terminal
       subtype is what stops an agent from getting its own genuine 0.0 excluded
       from the reward mean by emitting infra vocabulary. Quota is unaffected by
       this ordering: a real quota stub is stamped ``error_category='quota'`` by
       ``adapters/claude.py`` (whose ``_cli_origin_text`` already strips agent
       stream-json before matching), so it is caught structurally at step 1 —
       "quota wins over subtype" is enforced there, not here.
    5. A remaining ``status == 'error'`` row → INFRA_FAILURE: the executor's own
       infra-casualty label (crash, no result record, invalid model token).
    """
    if task.error_category in _INFRA_ERROR_CATEGORIES:
        return TrialClass.INFRA_FAILURE
    if task.verdict == "verifier_error":
        return TrialClass.INFRA_FAILURE
    if task.status == "completed" or task.scoring_details:
        return TrialClass.VALID
    if task.result_subtype in _TERMINAL_RESULT_SUBTYPES or task.status == "failed":
        return TrialClass.GENUINE_FAILURE
    if _matches_infra_text(task):
        return TrialClass.INFRA_FAILURE
    if task.status == "error":
        return TrialClass.INFRA_FAILURE
    return TrialClass.GENUINE_FAILURE


def is_infra_failure(task: CompletedTask) -> bool:
    """Return whether *task* is an infrastructure casualty.

    The reward-population predicate: infra casualties are excluded from
    mean_score / median / pass-rate / CIs and surfaced via a separate count,
    never silently rolled into the reward mean (generalizes ``is_quota_casualty``,
    codeprobe-a8r). Single source of truth so the summarizers, the paired
    hypothesis tests, and the validity gate agree on which trials count.
    """
    return classify_trial(task) is TrialClass.INFRA_FAILURE


def _trial_id(task: CompletedTask) -> str:
    """Stable per-trial identifier: ``<task_id>#rep<repeat_index>``."""
    return f"{task.task_id}#rep{task.repeat_index}"


@dataclass(frozen=True)
class ValidityReport:
    """Result of the infra-failure validity triage over a run's trials.

    ``passed`` is True only when zero infra failures remain unresolved. While
    it is False the run is not quotable: the offending trials must be re-run to
    ``completed`` (or explicitly reclassified genuine with a reason) first
    (codeprobe-77z).

    This is enforced, not advisory. ``codeprobe interpret`` raises a terminal
    ``VALIDITY_FAILED`` :class:`~codeprobe.cli.errors.DiagnosticError` and exits
    2 when ``passed`` is False, so a pipeline branching on the exit code cannot
    read an invalid run as a success (codeprobe-c4al). In-process callers that
    build a report themselves get no such gate for free and must check
    ``passed`` — see ``docs/conventions/validity-triage.md``.
    """

    passed: bool
    total_trials: int
    infra_failure_count: int
    infra_failure_trial_ids: tuple[str, ...]

    def summary(self) -> str:
        """One-line human-readable verdict, listing offending trial ids."""
        if self.passed:
            return f"VALIDITY PASS — 0 infra failures across {self.total_trials} trials"
        ids = ", ".join(self.infra_failure_trial_ids)
        return (
            f"VALIDITY FAIL — {self.infra_failure_count} unresolved infra "
            f"failure(s) of {self.total_trials} trials: {ids}. Re-run these to "
            "'completed' (or reclassify genuine with a reason) before treating "
            "the run as quotable."
        )


class ValidityTriage:
    """Single-pass accumulator for the gate.

    The streaming report path (``generate_report_streaming``) consumes each
    config's trials exactly once and never buffers them, so it observes trials
    here instead of re-walking a list. Only the infra trial ids are retained
    (a small set by construction), keeping the streaming path O(infra), not
    O(trials).
    """

    def __init__(self) -> None:
        self._total = 0
        self._infra_ids: list[str] = []

    def observe(self, task: CompletedTask) -> None:
        """Record one trial."""
        self._total += 1
        if is_infra_failure(task):
            self._infra_ids.append(_trial_id(task))

    def report(self) -> ValidityReport:
        """Return the gate verdict over every trial observed so far."""
        return ValidityReport(
            passed=not self._infra_ids,
            total_trials=self._total,
            infra_failure_count=len(self._infra_ids),
            infra_failure_trial_ids=tuple(self._infra_ids),
        )


def triage_run(tasks: Iterable[CompletedTask]) -> ValidityReport:
    """Run the infra-failure validity gate over a run's trials.

    Returns a :class:`ValidityReport` that FAILs when >=1 infra failure exists
    and PASSes at zero, surfacing the offending trial ids either way.
    """
    triage = ValidityTriage()
    for task in tasks:
        triage.observe(task)
    return triage.report()
