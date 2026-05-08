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

import pytest

from codeprobe.adapters.claude import _detect_quota_error


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

    @pytest.mark.parametrize(
        "phrase",
        [
            "monthly usage limit",
            "monthly  usage  limit",  # extra whitespace
            "rate limit reached",
            "Rate Limit Exceeded",
            "quota exceeded",
            "usage limit reached",
        ],
    )
    def test_wording_variants(self, phrase: str) -> None:
        assert _detect_quota_error(f"agent: {phrase} — halting", None) is not None


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
