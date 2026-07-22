"""Copilot quota / rate-limit detection (codeprobe-f7rl.29).

Quota exhaustion in the Copilot CLI prints a literal line instead of an
NDJSON event. The adapter must stamp ``error_category='quota'`` so the
executor halts dispatch — and must NOT misdiagnose the missing telemetry
as an outdated CLI. Agent-authored content mentioning rate limits must
never trigger the detector (same pitfall as codeprobe-9tk on Claude).
"""

from __future__ import annotations

import json
import subprocess

from codeprobe.adapters.copilot import CopilotAdapter


def _copilot_result(
    stdout: str, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["copilot"], returncode=0, stdout=stdout, stderr=stderr
    )


class TestCopilotQuotaDetection:
    def test_literal_rate_limit_line_sets_quota(self) -> None:
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant.message",
                        "data": {"content": "working", "outputTokens": 5},
                    }
                ),
                "Rate limit exceeded. Please try again later.",
            ]
        )
        output = CopilotAdapter().parse_output(_copilot_result(stdout), duration=1.0)
        assert output.error_category == "quota"
        assert output.error is not None
        assert output.error.startswith("quota/rate limit: ")
        assert "Rate limit exceeded" in output.error
        assert "upgrade" not in output.error.lower()

    def test_stderr_rate_limit_sets_quota(self) -> None:
        stdout = json.dumps(
            {
                "type": "assistant.message",
                "data": {"content": "hi", "outputTokens": 3},
            }
        )
        output = CopilotAdapter().parse_output(
            _copilot_result(stdout, stderr="transport: quota exceeded"),
            duration=1.0,
        )
        assert output.error_category == "quota"
        assert "quota exceeded" in output.error

    def test_quota_stub_replaces_upgrade_diagnosis(self) -> None:
        """A quota stub produces no outputTokens; the error must name the
        quota, not prescribe a CLI upgrade."""
        stdout = "rate limit reached for this session"
        output = CopilotAdapter().parse_output(_copilot_result(stdout), duration=1.0)
        assert output.error_category == "quota"
        assert "Upgrade" not in output.error
        assert "1.0.4" not in output.error

    def test_agent_event_content_does_not_trigger(self) -> None:
        """Rate-limit wording inside agent NDJSON events is agent content,
        not a CLI stub — it must not halt the run."""
        stdout = json.dumps(
            {
                "type": "assistant.message",
                "data": {
                    "content": "the API returned rate limit exceeded",
                    "outputTokens": 3,
                },
            }
        )
        output = CopilotAdapter().parse_output(_copilot_result(stdout), duration=1.0)
        assert output.error_category is None
