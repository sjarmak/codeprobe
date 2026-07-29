"""Regression tests for OAuth quota detection in the Claude adapter.

Background (codeprobe-9xrl): when the Claude Code OAuth account hits its
monthly usage limit, the CLI returns a 41-byte literal string —
``"You've hit your org's monthly usage limit"`` — instead of a JSON
envelope. Before this fix the adapter scored the resulting trial as
0.0, contaminating the run's mean_score. The fix detects the quota
pattern, sets ``error_category="quota"`` on ``AgentOutput``, and the
executor halts the run on first detection.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from codeprobe.adapters.claude import ClaudeAdapter, _detect_quota_error


class TestQuotaDetection:
    """``_detect_quota_error`` recognises wording variants."""

    def test_detects_oauth_monthly_limit(self) -> None:
        msg = "You've hit your org's monthly usage limit"
        assert _detect_quota_error(msg, None) is not None

    def test_detects_rate_limit_exceeded(self) -> None:
        msg = "API error: rate limit exceeded — try again in 5 minutes"
        assert _detect_quota_error(msg, None) is not None

    def test_detects_quota_exhausted(self) -> None:
        msg = "Your monthly quota exhausted; please upgrade."
        assert _detect_quota_error(msg, None) is not None

    def test_case_insensitive(self) -> None:
        msg = "MONTHLY USAGE LIMIT REACHED"
        assert _detect_quota_error(msg, None) is not None

    def test_returns_full_offending_line(self) -> None:
        stdout = (
            "Some preamble text.\n"
            "ERROR: monthly usage limit reached for organization xyz.\n"
            "Goodbye.\n"
        )
        result = _detect_quota_error(stdout, None)
        assert result is not None
        assert "monthly usage limit reached" in result
        # The full triggering line is returned (not just the regex match).
        assert "organization xyz" in result

    def test_checks_stderr_when_stdout_clean(self) -> None:
        result = _detect_quota_error(
            stdout="some real output",
            stderr="warning: rate limit exceeded for this account",
        )
        assert result is not None

    def test_no_match_returns_none(self) -> None:
        assert _detect_quota_error("normal stdout", "normal stderr") is None

    def test_empty_streams(self) -> None:
        assert _detect_quota_error("", None) is None
        assert _detect_quota_error("", "") is None

    def test_detects_session_limit_stub(self) -> None:
        """The 2026-06 OAuth wording (codeprobe-4cl6.3): the CLI returns a
        bare ``You've hit your session limit · resets …`` line instead of a
        JSON envelope. This poisoned the entire 4cl6 cap75 sweep undetected.
        """
        msg = "You've hit your session limit · resets 1:10pm (America/New_York)"
        result = _detect_quota_error(msg, None)
        assert result is not None
        assert "session limit" in result

    def test_bare_session_limit_prose_does_not_trigger(self) -> None:
        """Agent prose discussing session limits (common in gascity tasks,
        which edit session-management code) must NOT halt the run — only
        the anchored ``hit your session limit`` stub counts.
        """
        prose = "I will increase the session limit in the pool config."
        assert _detect_quota_error(prose, None) is None

    @pytest.mark.parametrize(
        "phrase",
        [
            "monthly usage limit",
            "monthly  usage  limit",  # extra whitespace
            "rate limit reached",
            "Rate Limit Exceeded",
            "quota exceeded",
            "usage limit reached",
            "hit your session limit",
            "You've HIT YOUR SESSION LIMIT",
        ],
    )
    def test_wording_variants(self, phrase: str) -> None:
        assert _detect_quota_error(f"agent: {phrase} — halting", None) is not None


class TestQuotaDetectionIgnoresAgentStreamJson:
    """codeprobe-9tk: the detector must scan only the CLI's literal output,
    not the agent's stream-json tool I/O. On the gascity corpus the agent
    edits/reasons about code containing "quota exhausted" / "session limit",
    which previously false-tripped the detector and halted a whole run.
    """

    def test_agent_tool_result_mentioning_quota_does_not_trigger(self) -> None:
        # A real stream-json ``type=user`` event whose tool_result content
        # is the agent editing gascity session/quota code — must NOT trigger.
        ev = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "content": (
                            "Updated session_reconciler.go: return "
                            "ErrQuotaExhausted when the monthly usage limit "
                            "is reached / quota exhausted."
                        ),
                    }
                ],
            },
        }
        stdout = json.dumps(ev)
        assert _detect_quota_error(stdout, None) is None

    def test_assistant_event_reasoning_about_quota_does_not_trigger(self) -> None:
        ev = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I'll handle the 'quota exceeded' branch."}
                ],
            },
        }
        assert _detect_quota_error(json.dumps(ev), None) is None

    def test_result_event_answer_mentioning_quota_does_not_trigger(self) -> None:
        # Even the CLI's ``type=result`` envelope carries the agent's final
        # answer (agent content), so a quota mention there is not a stub.
        ev = {"type": "result", "subtype": "success", "result": "Fixed quota exhausted handling."}
        assert _detect_quota_error(json.dumps(ev), None) is None

    def test_real_stub_amid_stream_json_still_detected(self) -> None:
        # The genuine OAuth stub is a NON-JSON literal line; it must still be
        # caught even when surrounded by agent stream-json events.
        lines = [
            json.dumps({"type": "user", "message": {"content": "editing quota code"}}),
            "You've hit your session limit · resets 1:10pm",
            json.dumps({"type": "assistant", "message": {"content": "done"}}),
        ]
        result = _detect_quota_error("\n".join(lines), None)
        assert result is not None
        assert "session limit" in result

    def test_real_stub_in_stderr_still_detected_with_jsonl_stdout(self) -> None:
        stdout = json.dumps(
            {"type": "user", "message": {"content": "the agent wrote quota exhausted"}}
        )
        result = _detect_quota_error(stdout, "transport: rate limit exceeded")
        assert result is not None


class TestAgentOutputErrorCategoryField:
    """``AgentOutput.error_category`` carries the adapter's classification."""

    def test_default_is_none(self) -> None:
        from codeprobe.adapters.protocol import AgentOutput

        out = AgentOutput(
            stdout="ok",
            stderr=None,
            exit_code=0,
            duration_seconds=1.0,
        )
        assert out.error_category is None

    def test_quota_category_round_trips(self) -> None:
        from codeprobe.adapters.protocol import AgentOutput

        out = AgentOutput(
            stdout="",
            stderr=None,
            exit_code=0,
            duration_seconds=1.0,
            error="OAuth quota exhausted",
            error_category="quota",
        )
        assert out.error_category == "quota"
        assert out.error == "OAuth quota exhausted"


class TestExecutorRoutesQuotaCategory:
    """``execute_task`` propagates ``output.error_category`` onto the
    completed task when set, instead of hardcoding ``"agent"``.
    """

    def test_quota_category_propagates_to_completed_task(
        self, tmp_path
    ) -> None:
        from codeprobe.adapters.protocol import AgentConfig, AgentOutput
        from codeprobe.core.executor import execute_task
        from tests.conftest import FakeAdapter

        # Build a minimal task fixture.
        task_dir = tmp_path / "task-quota"
        task_dir.mkdir()
        (task_dir / "instruction.md").write_text("Do the thing.")
        tests_dir = task_dir / "tests"
        tests_dir.mkdir()
        (tests_dir / "test.sh").write_text("#!/bin/bash\nexit 0\n")

        # Stub adapter that simulates the quota response.
        class _QuotaAdapter(FakeAdapter):
            def run(
                self,
                prompt: str,
                config: AgentConfig,
                session_env: dict[str, str] | None = None,
            ) -> AgentOutput:
                return AgentOutput(
                    stdout="You've hit your org's monthly usage limit",
                    stderr=None,
                    exit_code=0,
                    duration_seconds=0.05,
                    error="OAuth quota exhausted: ...",
                    error_category="quota",
                )

        adapter = _QuotaAdapter(stdout="ignored")
        result = execute_task(adapter, task_dir, tmp_path, AgentConfig())
        assert result.completed.error_category == "quota"
        assert result.completed.status == "error"
        assert result.completed.automated_score == 0.0

    def test_default_category_falls_back_to_agent(self, tmp_path) -> None:
        """When the adapter does NOT declare a category, executor uses 'agent'."""
        from codeprobe.adapters.protocol import AgentConfig, AgentOutput
        from codeprobe.core.executor import execute_task
        from tests.conftest import FakeAdapter

        task_dir = tmp_path / "task-generic"
        task_dir.mkdir()
        (task_dir / "instruction.md").write_text("Do the thing.")
        tests_dir = task_dir / "tests"
        tests_dir.mkdir()
        (tests_dir / "test.sh").write_text("#!/bin/bash\nexit 0\n")

        class _GenericErrorAdapter(FakeAdapter):
            def run(
                self,
                prompt: str,
                config: AgentConfig,
                session_env: dict[str, str] | None = None,
            ) -> AgentOutput:
                return AgentOutput(
                    stdout="some content",
                    stderr=None,
                    exit_code=0,
                    duration_seconds=0.05,
                    error="generic agent failure",
                    # error_category intentionally omitted
                )

        adapter = _GenericErrorAdapter(stdout="ignored")
        result = execute_task(adapter, task_dir, tmp_path, AgentConfig())
        assert result.completed.error_category == "agent"


# The exact literal the Claude CLI returned in
# runs/r0-file-read-seed1/rich/seed1/expB — three trials received it and were
# recorded status=error / error_category=agent / quota_error_count=0, so the
# quota condition bypassed quota classification and campaign summaries and
# retry/operator decisions were misleading (codeprobe-ymxn). The note is
# always "· resets <time>"; the time carries no colon here ("12pm"), unlike
# the earlier fixture's "1:10pm", so both punctuation shapes are pinned.
_EXPB_SESSION_LIMIT_STUB = (
    "You've hit your session limit · resets 12pm (America/New_York)"
)


class TestParseOutputClassifiesSessionLimitStub:
    """End-to-end ``ClaudeAdapter.parse_output`` coverage for the session-limit
    stub — the earlier tests exercise only the ``_detect_quota_error`` regex
    helper, not the full parse pipeline that also decides ``error_terminal``.

    ``error_terminal`` is the retry lever: a quota casualty is stamped
    ``error_terminal=False`` so it is NEVER banked as a genuine 0.0 terminal
    failure. The executor halts the run on the first quota detection
    (``executor.py`` _handle_result), the trial is excluded from the reward
    population as an infra casualty and surfaced via ``quota_error_count``
    (``analysis/stats.is_quota_casualty``), and it is re-run after the quota
    resets rather than counted against the arm — see
    ``docs/conventions/validity-triage.md``. Banking it as terminal instead
    would drag the arm's mean toward zero on an artifact that never measured
    solution quality.
    """

    def test_exact_expB_stub_is_quota_and_nonterminal(self) -> None:
        result = subprocess.CompletedProcess(
            args=["claude"],
            returncode=0,
            stdout=_EXPB_SESSION_LIMIT_STUB + "\n",
            stderr="",
        )
        out = ClaudeAdapter().parse_output(result, duration=1.28)

        assert out.error_category == "quota"
        # Not terminal → re-run after reset, never banked as a genuine 0.0.
        assert out.error_terminal is False
        # Transcript / diagnostics preserved: the verbatim provider wording is
        # carried on both the surfaced error and the stdout transcript.
        assert _EXPB_SESSION_LIMIT_STUB in (out.error or "")
        assert _EXPB_SESSION_LIMIT_STUB in out.stdout

    @pytest.mark.parametrize(
        "reset_suffix",
        [
            "· resets 12pm (America/New_York)",  # no colon (the expB wording)
            "· resets 1:10pm",  # colon + minutes
            "· resets 9am",  # bare hour
            "",  # no reset note at all
        ],
    )
    def test_reset_suffix_variants_are_quota(self, reset_suffix: str) -> None:
        """The classification is anchored on ``hit your session limit`` and is
        invariant to the reset-note punctuation / time format that follows."""
        stub = f"You've hit your session limit {reset_suffix}".strip()
        result = subprocess.CompletedProcess(
            args=["claude"], returncode=0, stdout=stub + "\n", stderr=""
        )
        out = ClaudeAdapter().parse_output(result, duration=1.0)
        assert out.error_category == "quota"
        assert out.error_terminal is False

    def test_quota_wins_over_terminal_result_envelope(self) -> None:
        """A quota stub alongside a terminal-looking ``result`` envelope stays
        quota + non-terminal: the casualty must be re-run, not banked as a
        genuine ``error_max_turns`` 0.0 (``claude.py`` "quota wins over
        subtype")."""
        result_ev = json.dumps(
            {
                "type": "result",
                "subtype": "error_max_turns",
                "is_error": True,
                "result": "stopped",
                "num_turns": 30,
            }
        )
        result = subprocess.CompletedProcess(
            args=["claude"],
            returncode=0,
            stdout=f"{_EXPB_SESSION_LIMIT_STUB}\n{result_ev}\n",
            stderr="",
        )
        out = ClaudeAdapter().parse_output(result, duration=1.0)
        assert out.error_category == "quota"
        assert out.error_terminal is False
        assert _EXPB_SESSION_LIMIT_STUB in (out.error or "")

    def test_transport_rate_limit_in_stderr_is_quota(self) -> None:
        """A transport-layer rate-limit surfaced on stderr (clean stdout) is
        also classified quota, not a generic agent failure."""
        result = subprocess.CompletedProcess(
            args=["claude"],
            returncode=0,
            stdout="regular agent output\n",
            stderr="transport: rate limit exceeded",
        )
        out = ClaudeAdapter().parse_output(result, duration=1.0)
        assert out.error_category == "quota"
        assert out.error_terminal is False
