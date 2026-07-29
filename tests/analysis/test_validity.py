"""Tests for the infra-failure validity triage gate (codeprobe-77z).

Covers the structural classifier (``classify_trial`` / ``is_infra_failure``),
the run-level gate (``triage_run`` / ``ValidityReport``), the aggregation
exclusion in both summarizers, and the report rendering. The motivating
regression is the codeprobe-9tk / 0d4ec3ad crash: a ``with-sg-full`` trial
overran the 32000-output-token CLI ceiling, was recorded ``status=error`` with
empty ``scoring_details`` and ``automated_score=0.0``, and dragged that arm's
mean 0.72 -> 0.645 — an infra casualty counted as a genuine bad solution.
"""

from __future__ import annotations

import json

from codeprobe.analysis.report import (
    format_json_report,
    format_text_report,
    generate_report,
)
from codeprobe.analysis.stats import (
    is_quota_casualty,
    is_scorable_run,
    summarize_completed_tasks,
    summarize_config,
)
from codeprobe.analysis.validity import (
    TrialClass,
    ValidityReport,
    _matches_infra_text,
    classify_trial,
    is_infra_failure,
    triage_run,
)
from codeprobe.models.experiment import CompletedTask, ConfigResults

# ---------------------------------------------------------------------------
# Fixtures — the three canonical trial shapes the gate must tell apart (A5)
# ---------------------------------------------------------------------------


def _token_ceiling_crash(task_id: str = "crash", repeat: int = 0) -> CompletedTask:
    """The 0d4ec3ad-class crash: token-ceiling overrun, empty scoring (A1)."""
    return CompletedTask(
        task_id=task_id,
        automated_score=0.0,
        repeat_index=repeat,
        status="error",
        error_category="agent",
        metadata={"error": "API Error: exceeded the 32000 output token maximum"},
    )


def _scored_trial(task_id: str = "scored", score: float = 0.0) -> CompletedTask:
    """A real measurement that ran scoring end-to-end.

    The explicit ``passed`` flag tracks the score so pass-rate and mean agree
    (a low score is a real data point, never an infra casualty).
    """
    return CompletedTask(
        task_id=task_id,
        automated_score=score,
        status="completed",
        scoring_details={"passed": score >= 0.5, "scorer_family": "binary"},
    )


def _verdict_trial(verdict: str, task_id: str = "verdict") -> CompletedTask:
    """A scored row whose typed verdict determines measurement validity."""
    return CompletedTask(
        task_id=task_id,
        automated_score=0.0,
        status="completed",
        verdict=verdict,
        scoring_details={"passed": False},
    )


def _genuine_terminal_failure(task_id: str = "maxturns") -> CompletedTask:
    """error_max_turns with no answer: a genuine 0.0, NOT infra.

    Kept distinct from cap-contamination (codeprobe-8up).
    """
    return CompletedTask(
        task_id=task_id,
        automated_score=0.0,
        status="failed",
        error_category="agent",
        result_subtype="error_max_turns",
        metadata={"error": "agent stopped after reaching the max turn limit"},
    )


def _quota_casualty(task_id: str = "quota") -> CompletedTask:
    """A quota-errored casualty — the codeprobe-a8r subset of infra."""
    return CompletedTask(
        task_id=task_id,
        automated_score=0.0,
        status="error",
        error_category="quota",
        metadata={"error": "OAuth quota exhausted: hit your session limit"},
    )


def _crash_with_error(task_id: str, error: str) -> CompletedTask:
    """A crashed row carrying *error* as its recorded fault text.

    ``status='error'`` — the shape the executor actually emits for a crash.
    ``executor.py:828`` projects ``status='failed'`` **only** when the adapter
    set ``error_terminal``, and ``claude.py:567`` sets that flag only for
    ``result_subtype == 'error_max_turns'`` (and never under quota). So a
    crashed trial always lands on ``'error'``; a ``'failed'`` row is always a
    genuine turn-cap. An earlier revision of these tests used a synthetic
    ``status='failed'`` + no-subtype row, a shape no adapter can produce, which
    had the effect of pinning "a cap-out whose narration mentions infra is an
    infra casualty" as the contract — the exact misclassification that lets an
    evaluated agent drop its own genuine 0.0 out of the reward mean.
    """
    return CompletedTask(
        task_id=task_id,
        automated_score=0.0,
        status="error",
        error_category="agent",
        metadata={"error": error},
    )


# ---------------------------------------------------------------------------
# Classifier — A1, A4 (structural only)
# ---------------------------------------------------------------------------


class TestClassifyTrial:
    def test_token_ceiling_crash_is_infra_not_genuine(self) -> None:
        """A1: token-ceiling + empty scoring → INFRA_FAILURE, never a real 0."""
        t = _token_ceiling_crash()
        assert classify_trial(t) is TrialClass.INFRA_FAILURE
        assert is_infra_failure(t) is True

    def test_scored_trial_stays_valid(self) -> None:
        """ZFC guard: a genuinely low score is a real data point.

        It is NOT reclassified as infra — no score threshold is applied.
        """
        assert classify_trial(_scored_trial(score=0.0)) is TrialClass.VALID
        assert classify_trial(_scored_trial(score=0.08)) is TrialClass.VALID
        assert is_infra_failure(_scored_trial()) is False

    def test_verifier_error_is_infra_but_incorrect_is_valid(self) -> None:
        """A broken verifier is excluded; a genuine wrong answer remains."""
        verifier_error = _verdict_trial("verifier_error")
        incorrect = _verdict_trial("incorrect")

        assert classify_trial(verifier_error) is TrialClass.INFRA_FAILURE
        assert is_scorable_run(verifier_error) is False
        assert classify_trial(incorrect) is TrialClass.VALID
        assert is_scorable_run(incorrect) is True

    def test_error_max_turns_is_genuine_failure(self) -> None:
        """A terminal ``failed`` cap-hit is a genuine 0.0 that stays in the
        reward population — distinct from an infra crash."""
        t = _genuine_terminal_failure()
        assert classify_trial(t) is TrialClass.GENUINE_FAILURE
        assert is_infra_failure(t) is False

    def test_error_status_with_terminal_subtype_is_genuine(self) -> None:
        """Review blocker (mayor REQUEST_CHANGES): a ``status='error'`` row is
        NOT blindly infra — an adapter-declared terminal ``result_subtype``
        (the CompletedTask-level proxy for ``AgentOutput.error_terminal``,
        executor.py:828) means the agent DID get its turns, so re-running it
        would just burn budget on the same cap."""
        t = CompletedTask(
            task_id="t",
            automated_score=0.0,
            status="error",
            error_category="agent",
            result_subtype="error_max_turns",
            metadata={"error": "reached the turn limit"},
        )
        assert classify_trial(t) is TrialClass.GENUINE_FAILURE
        assert is_infra_failure(t) is False

    def test_quota_wins_over_terminal_subtype(self) -> None:
        """Quota beats a terminal subtype (mirrors claude.py's own precedence):
        a quota stub is an infra casualty even when a result envelope also
        reported ``error_max_turns``."""
        t = CompletedTask(
            task_id="t",
            automated_score=0.0,
            status="error",
            error_category="quota",
            result_subtype="error_max_turns",
            metadata={"error": "OAuth quota exhausted"},
        )
        assert classify_trial(t) is TrialClass.INFRA_FAILURE

    def test_quota_is_infra(self) -> None:
        """Quota casualty (a8r) is one species of infra failure."""
        assert classify_trial(_quota_casualty()) is TrialClass.INFRA_FAILURE
        assert is_infra_failure(_quota_casualty()) is True

    def test_infra_error_category_outranks_completed_status(self) -> None:
        """The executor never stamps an ``error_category`` on a row that
        actually scored, so an infra category outranks a ``completed`` status.
        This is what keeps ``is_infra_failure`` a strict superset of
        ``is_quota_casualty`` for the executor-stamped quota rows the
        codeprobe-9jxx fixtures encode (status defaults to ``completed``)."""
        t = CompletedTask(
            task_id="t",
            automated_score=0.0,
            status="completed",
            error_category="quota",
        )
        assert is_quota_casualty(t) is True
        assert classify_trial(t) is TrialClass.INFRA_FAILURE
        assert is_scorable_run(t) is False

    def test_terminal_failed_with_infra_text_is_infra(self) -> None:
        """A token-ceiling crash recorded as terminal ``failed`` is still
        caught by the error-text signature, not just the ``error`` status."""
        t = _crash_with_error("t", "exceeded the 16000 output token maximum")
        assert classify_trial(t) is TrialClass.INFRA_FAILURE

    def test_completed_with_scoring_is_valid_even_with_noisy_metadata(self) -> None:
        """If scoring ran end-to-end the trial is VALID regardless of stray
        text — a real measurement is never thrown away."""
        t = CompletedTask(
            task_id="t",
            automated_score=0.3,
            status="completed",
            scoring_details={"passed": False, "scorer_family": "oracle_overlap_f1"},
            metadata={"error": "transient rate limit retried then recovered"},
        )
        assert classify_trial(t) is TrialClass.VALID
        assert is_infra_failure(t) is False

    def test_error_status_but_scoring_ran_is_valid(self) -> None:
        """If scoring ran end-to-end (non-empty scoring_details) the trial is a
        real measurement even if a stale ``status='error'`` lingers — the
        end-to-end check takes priority over the status label."""
        t = CompletedTask(
            task_id="t",
            automated_score=0.4,
            status="error",
            scoring_details={"passed": False, "scorer_family": "binary"},
        )
        assert classify_trial(t) is TrialClass.VALID
        assert is_infra_failure(t) is False

    def test_infra_text_signatures(self) -> None:
        """Each documented infra marker in a terminal-failed row is caught."""
        markers = [
            "API Error 529 overloaded",
            "rate limit exceeded",
            "Request was rate-limited",
            "OAuth token refresh failed",
            "Claude AI usage limit reached",
            "hit your session limit",
            "quota exhausted for this account",
            "budget exceeded for this run",
            "connection refused by host",
            "connection reset by peer",
            "network is unreachable",
            "failed to connect to MCP server sourcegraph",
            'MCP server "sourcegraph": connection failed',
            "request timed out after 3600s",
            "exceeded the 32000 output token maximum",
        ]
        for msg in markers:
            assert classify_trial(_crash_with_error("t", msg)) is (
                TrialClass.INFRA_FAILURE
            ), msg

    def test_infra_markers_do_not_fire_on_unrelated_text(self) -> None:
        """The markers are word-anchored: vocabulary that merely *contains* a
        marker substring must not read as an infra signature.

        Asserted against ``_matches_infra_text`` directly rather than through
        ``classify_trial``. The text scan is the classifier's last-resort
        signal, below every structural one, so on any row the executor can
        actually emit some structural rule decides first and the marker regex
        never gets to speak — routing this through ``classify_trial`` would
        assert the structural rule, not the anchoring, and would pass even if
        the regex were hopelessly greedy.
        """
        benign = [
            "agent stopped after reaching the max turn limit",
            "the rate limiter tests failed",  # 'rate limiter' != 'rate limit'
            "OpenAPI errors in the generated client",  # 'openapi error'
            "quota tracking module has a syntax error",
            "budget calculation returned None",
            "connection pool was misconfigured",
            "the agent produced no answer.txt",
            "the halting problem is undecidable",
            "graceful halting of the worker pool",
        ]
        for msg in benign:
            assert not _matches_infra_text(_crash_with_error("t", msg)), msg

    def test_infra_markers_fire_on_marker_text(self) -> None:
        """The positive half of the anchoring contract, asserted at the same
        level as its negative twin above."""
        for msg in (
            "API Error 529 overloaded",
            "rate limit exceeded",
            "connection refused by host",
            "exceeded the 32000 output token maximum",
        ):
            assert _matches_infra_text(_crash_with_error("t", msg)), msg

    def test_infra_error_categories(self) -> None:
        """timeout / system / quota error categories are infra regardless of
        text — the executor already classified the fault (executor._classify_error)."""
        for cat in ("timeout", "system", "quota", "auth_failure"):
            t = CompletedTask(
                task_id="t",
                automated_score=0.0,
                status="failed",
                error_category=cat,
            )
            assert classify_trial(t) is TrialClass.INFRA_FAILURE, cat

    def test_agent_error_category_alone_is_not_infra(self) -> None:
        """``agent`` is the executor's ambiguous fallback category — it must
        not by itself mark a terminal failure as infra."""
        t = CompletedTask(
            task_id="t", automated_score=0.0, status="failed", error_category="agent"
        )
        assert classify_trial(t) is TrialClass.GENUINE_FAILURE

    def test_is_infra_failure_is_superset_of_is_quota_casualty(self) -> None:
        """Every quota casualty is an infra failure; the converse need not
        hold (token-ceiling is infra but not quota)."""
        assert is_quota_casualty(_quota_casualty()) is True
        assert is_infra_failure(_quota_casualty()) is True
        crash = _token_ceiling_crash()
        assert is_quota_casualty(crash) is False
        assert is_infra_failure(crash) is True


# ---------------------------------------------------------------------------
# Reward population — the widened is_scorable_run (codeprobe-h3j4 + 77z)
# ---------------------------------------------------------------------------


class TestScorableRunHonoursInfraGate:
    def test_infra_text_on_failed_row_leaves_reward_population(self) -> None:
        """The remaining contamination hole h3j4 left open: a crash recorded as
        terminal ``failed`` (not ``error``) used to keep its 0.0 in the mean."""
        t = _crash_with_error("t", "API Error: exceeded the 32000 output token maximum")
        assert is_scorable_run(t) is False

    def test_genuine_terminal_failure_stays_in_reward_population(self) -> None:
        """The opposite-bug guard: a real terminal 0.0 must stay in the mean —
        excluding it would hide real failures (codeprobe-8up / h3j4)."""
        assert is_scorable_run(_genuine_terminal_failure()) is True

    def test_completed_run_stays_in_reward_population(self) -> None:
        assert is_scorable_run(_scored_trial(score=0.0)) is True


# ---------------------------------------------------------------------------
# Validity gate — A3
# ---------------------------------------------------------------------------


class TestTriageRun:
    def test_fail_with_unresolved_infra_lists_ids(self) -> None:
        """A3: >=1 infra failure → FAIL, surfacing the offending trial ids."""
        rep = triage_run(
            [
                _token_ceiling_crash("crash", repeat=1),
                _scored_trial("low"),
                _quota_casualty("quota"),
            ]
        )
        assert rep.passed is False
        assert rep.infra_failure_count == 2
        assert rep.total_trials == 3
        assert rep.infra_failure_trial_ids == ("crash#rep1", "quota#rep0")
        assert "VALIDITY FAIL" in rep.summary()
        assert "crash#rep1" in rep.summary()

    def test_pass_at_zero_infra(self) -> None:
        """A3: a run with only valid / genuine trials PASSes."""
        rep = triage_run([_scored_trial("low"), _genuine_terminal_failure("mt")])
        assert rep.passed is True
        assert rep.infra_failure_count == 0
        assert rep.infra_failure_trial_ids == ()
        assert "VALIDITY PASS" in rep.summary()

    def test_empty_run_passes(self) -> None:
        rep = triage_run([])
        assert rep == ValidityReport(
            passed=True,
            total_trials=0,
            infra_failure_count=0,
            infra_failure_trial_ids=(),
        )


# ---------------------------------------------------------------------------
# Aggregation exclusion — A2
# ---------------------------------------------------------------------------


class TestInfraExclusionFromAggregation:
    def _mixed(self) -> list[CompletedTask]:
        # 1 token-ceiling crash + real trials 1.0, 1.0, 0.0.
        # Reward population is the 3 real trials: mean=2/3, pass_rate=2/3.
        # If the infra 0.0 leaked in: mean would be 2/4=0.5.
        return [
            _token_ceiling_crash("crash"),
            _scored_trial("r1", score=1.0),
            _scored_trial("r2", score=1.0),
            _scored_trial("r3", score=0.0),
        ]

    def test_summarize_config_excludes_infra_from_mean(self) -> None:
        s = summarize_config(ConfigResults(config="cfg", completed=self._mixed()))
        assert s.mean_score == 2 / 3
        assert s.pass_rate == 2 / 3
        assert s.infra_failure_count == 1
        # Non-quota infra: the quota sub-count stays 0 (codeprobe-9xrl contract).
        assert s.quota_error_count == 0
        # Structural counts keep all 4 trials.
        assert s.total_tasks == 4
        assert s.errored == 1
        assert s.scored_count == 3

    def test_streaming_summarizer_agrees(self) -> None:
        batch = summarize_config(ConfigResults(config="cfg", completed=self._mixed()))
        stream = summarize_completed_tasks("cfg", iter(self._mixed()))
        assert batch.mean_score == stream.mean_score
        assert batch.pass_rate == stream.pass_rate
        assert batch.median_score == stream.median_score
        assert batch.infra_failure_count == stream.infra_failure_count
        assert batch.quota_error_count == stream.quota_error_count
        assert batch.errored_count == stream.errored_count

    def test_infra_failed_row_excluded_by_both_summarizers(self) -> None:
        """The h3j4 hole: a crash stamped terminal ``failed`` must leave the
        mean under BOTH summarizers."""
        tasks = [
            _crash_with_error("c1", "API Error: exceeded the 32000 output token maximum"),
            _scored_trial("r1", score=1.0),
        ]
        for s in (
            summarize_config(ConfigResults(config="cfg", completed=tasks)),
            summarize_completed_tasks("cfg", iter(tasks)),
        ):
            assert s.mean_score == 1.0
            assert s.infra_failure_count == 1
            assert s.scored_count == 1

    def test_genuine_terminal_failure_still_counts_toward_mean(self) -> None:
        """Guard against over-exclusion: a real terminal 0.0 stays in the mean."""
        tasks = [_genuine_terminal_failure("mt"), _scored_trial("r1", score=1.0)]
        for s in (
            summarize_config(ConfigResults(config="cfg", completed=tasks)),
            summarize_completed_tasks("cfg", iter(tasks)),
        ):
            assert s.mean_score == 0.5
            assert s.infra_failure_count == 0
            assert s.scored_count == 2

    def test_quota_counted_in_both_infra_and_quota_subcount(self) -> None:
        """A quota casualty raises infra_failure_count AND quota_error_count;
        a token-ceiling crash raises only infra_failure_count."""
        tasks = [
            _quota_casualty("q1"),
            _token_ceiling_crash("c1"),
            _scored_trial("r1", score=1.0),
        ]
        for s in (
            summarize_config(ConfigResults(config="cfg", completed=tasks)),
            summarize_completed_tasks("cfg", iter(tasks)),
        ):
            assert s.infra_failure_count == 2
            assert s.quota_error_count == 1
            assert s.mean_score == 1.0  # only the single real trial

    def test_all_infra_reports_no_reward_signal(self) -> None:
        """Every trial an infra casualty → zeroed stats, no crash, count
        surfaced."""
        tasks = [_token_ceiling_crash("c1"), _quota_casualty("q1")]
        for s in (
            summarize_config(ConfigResults(config="cfg", completed=tasks)),
            summarize_completed_tasks("cfg", iter(tasks)),
        ):
            assert s.mean_score == 0.0
            assert s.pass_rate == 0.0
            assert s.infra_failure_count == 2
            assert s.total_tasks == 2
            assert s.scored_count == 0


# ---------------------------------------------------------------------------
# Report rendering — A2/A3 visibility (never silent)
# ---------------------------------------------------------------------------


class TestReportSurfacesValidity:
    def _report(self):
        arm_a = ConfigResults(
            config="arm-A",
            completed=[
                _scored_trial("t1", score=1.0),
                _scored_trial("t2", score=0.0),
                _token_ceiling_crash("t3"),
            ],
        )
        arm_b = ConfigResults(
            config="arm-B",
            completed=[
                _scored_trial("t1", score=1.0),
                _scored_trial("t2", score=1.0),
            ],
        )
        return generate_report("demo", [arm_a, arm_b])

    def test_report_carries_validity_gate(self) -> None:
        report = self._report()
        assert report.validity is not None
        assert report.validity.passed is False
        assert report.validity.infra_failure_trial_ids == ("t3#rep0",)

    def test_text_report_shows_fail_verdict_and_ids(self) -> None:
        text = format_text_report(self._report())
        assert "### Validity" in text
        assert "VALIDITY FAIL" in text
        assert "t3#rep0" in text
        assert "NOT quotable" in text
        # The arm that lost a trial flags its non-quota infra count beside N.
        assert "infra failure(s)" in text

    def test_json_report_includes_validity(self) -> None:
        data = json.loads(format_json_report(self._report()))
        assert data["validity"]["passed"] is False
        assert data["validity"]["infra_failure_count"] == 1
        assert data["validity"]["infra_failure_trial_ids"] == ["t3#rep0"]
        # Per-arm infra count is serialized too (A2).
        by_label = {s["label"]: s for s in data["summaries"]}
        assert by_label["arm-A"]["infra_failure_count"] == 1
        assert by_label["arm-B"]["infra_failure_count"] == 0

    def test_clean_run_passes_gate(self) -> None:
        """A5: a clean run (no infra) PASSes and says so."""
        clean = ConfigResults(
            config="arm",
            completed=[_scored_trial("t1", score=1.0), _genuine_terminal_failure()],
        )
        report = generate_report("clean", [clean])
        assert report.validity is not None
        assert report.validity.passed is True
        text = format_text_report(report)
        assert "VALIDITY PASS" in text
        assert "NOT quotable" not in text


# ---------------------------------------------------------------------------
# Precedence: structural signals outrank the agent-influenceable error text
#
# ``metadata['error']`` is the adapter's verbatim ``envelope['result']`` (see
# ``adapters/telemetry.py:_extract_envelope_error``) — free text the evaluated
# agent can influence. If the text scan outranked the structural terminal
# subtype, an agent could get its own genuine 0.0 dropped from the reward mean
# just by emitting infra vocabulary. These tests pin the ordering that stops it.
# ---------------------------------------------------------------------------


def _max_turns_with_infra_text(error: str) -> CompletedTask:
    """A genuine max-turns failure whose free-text error carries infra words."""
    return CompletedTask(
        task_id="max-turns",
        automated_score=0.0,
        repeat_index=0,
        status="failed",
        result_subtype="error_max_turns",
        metadata={"error": error},
    )


def test_terminal_subtype_outranks_agent_influenceable_error_text() -> None:
    """A real cap-out stays GENUINE even if its error text screams 'infra'.

    Regression guard: reward-mean exclusion must not be reachable by writing
    infra vocabulary into a field the agent under evaluation can influence.
    """
    for text in (
        "connection refused",
        "API Error: exceeded the 32000 output token maximum",
        "rate limit exceeded",
        "network timeout",
    ):
        task = _max_turns_with_infra_text(text)
        assert classify_trial(task) is TrialClass.GENUINE_FAILURE, text
        assert not is_infra_failure(task), text


def test_quota_still_wins_over_terminal_subtype() -> None:
    """Reordering must not regress the quota contract (codeprobe-9jxx/a8r).

    A real quota stub is stamped structurally by the adapter
    (``error_category='quota'``, whose detection already strips agent
    stream-json via ``_cli_origin_text``), so it is caught at step 1 — ahead of
    the terminal subtype — without relying on the free-text scan.
    """
    task = CompletedTask(
        task_id="quota-and-cap",
        automated_score=0.0,
        repeat_index=0,
        status="failed",
        result_subtype="error_max_turns",
        error_category="quota",
        metadata={"error": "Claude usage limit reached"},
    )
    assert classify_trial(task) is TrialClass.INFRA_FAILURE
    assert is_infra_failure(task)


def test_infra_text_still_classifies_untyped_error_rows() -> None:
    """The text scan is still load-bearing where no structural signal exists."""
    task = CompletedTask(
        task_id="bare-crash",
        automated_score=0.0,
        repeat_index=0,
        status="error",
        metadata={"error": "API Error: exceeded the 32000 output token maximum"},
    )
    assert classify_trial(task) is TrialClass.INFRA_FAILURE


# ---------------------------------------------------------------------------
# Boundary hardening: a corrupt row must not abort the whole run's report
# ---------------------------------------------------------------------------


def test_malformed_metadata_does_not_crash_classification() -> None:
    """Non-dict ``metadata`` is tolerated, not fatal.

    ``triage_run`` walks every trial to build one report, so an uncaught
    ``AttributeError`` on a single torn/hand-edited checkpoint row would take
    down report generation for the entire run (CLAUDE.md: validate-or-die on
    data boundaries; never crash silently).
    """
    for bad in (["not", "a", "dict"], "a string", 42):
        task = CompletedTask(
            task_id="corrupt",
            automated_score=0.0,
            repeat_index=0,
            status="error",
            metadata=bad,  # type: ignore[arg-type]
        )
        assert classify_trial(task) is TrialClass.INFRA_FAILURE
        report = triage_run([task])
        assert isinstance(report, ValidityReport)
        assert report.infra_failure_count == 1


# ---------------------------------------------------------------------------
# Exclusions are never silent on ANY summary surface (codeprobe-77z scope)
#
# The widened exclusion reached every summarizer, but the *count* only reached
# `codeprobe interpret`. `run`'s envelope and experiment.json folded infra
# casualties into the generic `errored_count`, so a crash-inflated mean read
# identically to a clean one on the surfaces users actually look at.
# ---------------------------------------------------------------------------


def test_every_summary_surface_reports_the_infra_count() -> None:
    """run envelope, experiment.json, and ConfigSummary all name the subset."""
    from codeprobe.cli.run_cmd import build_run_envelope_summary
    from codeprobe.core.experiment import _compute_summary

    tasks = [
        _token_ceiling_crash("c1"),
        _quota_casualty("q1"),
        _genuine_terminal_failure("m1"),
        _scored_trial("s1", score=1.0),
    ]
    # 2 infra (crash + quota); the cap-out and the scored trial are real data.
    expected_infra = 2

    envelope, _, _ = build_run_envelope_summary({"cfg": tasks})
    assert envelope[0]["infra_failure_count"] == expected_infra

    assert _compute_summary(tasks)["infra_failure_count"] == expected_infra

    summary = summarize_config(ConfigResults(config="cfg", completed=tasks))
    assert summary.infra_failure_count == expected_infra


def test_verifier_error_moves_all_summary_counts_in_step() -> None:
    """The typed verdict reaches every existing reward/exclusion aggregate."""
    from codeprobe.cli.run_cmd import build_run_envelope_summary
    from codeprobe.core.experiment import _compute_summary

    tasks = [_verdict_trial("verifier_error", "broken"), _verdict_trial("incorrect")]

    envelope, _, _ = build_run_envelope_summary({"cfg": tasks})
    assert envelope[0]["infra_failure_count"] == 1
    assert envelope[0]["errored_count"] == 1
    assert envelope[0]["scored_count"] == 1
    assert envelope[0]["mean_score"] == 0.0

    aggregate = _compute_summary(tasks)
    assert aggregate["infra_failure_count"] == 1
    assert aggregate["errored_count"] == 1
    assert aggregate["tasks_completed"] == 2
    assert aggregate["mean_automated_score"] == 0.0


def test_exclusion_counts_nest_on_the_run_envelope() -> None:
    """quota ⊆ infra ⊆ errored — the containment the reports imply."""
    from codeprobe.cli.run_cmd import build_run_envelope_summary

    tasks = [
        _quota_casualty("q1"),
        _token_ceiling_crash("c1"),
        _genuine_terminal_failure("m1"),
        _scored_trial("s1", score=1.0),
    ]
    row, _, _ = build_run_envelope_summary({"cfg": tasks})
    quota, infra, errored = (
        row[0]["quota_error_count"],
        row[0]["infra_failure_count"],
        row[0]["errored_count"],
    )
    assert quota <= infra <= errored
    assert (quota, infra, errored) == (1, 2, 2)
    # The cap-out is NOT excluded: a genuine 0.0 stays in the reward population.
    assert row[0]["scored_count"] == 2
