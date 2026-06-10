"""Contract tests — every adapter output must populate cost/token fields.

Two layers:

* ``TestAdapterOutputContract`` — shape assertions on hand-built
  ``AgentOutput`` fixtures (documents the happy-path field contract).
* ``Test*ParseContract`` classes — feed real transcripts through each
  adapter's actual parse path (``parse_output`` / ``run``) and assert the
  cost-source honesty matrix: populated telemetry when the vendor reports
  usage, an explicit ``cost_source="unavailable"`` + error when it
  doesn't. This is what actually guards the "ALL adapters extract
  token/cost data" invariant — fixtures alone can't catch an adapter
  whose parser silently stops extracting.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from codeprobe.adapters.protocol import AgentConfig, AgentOutput

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _completed(
    stdout: str, returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["agent"], returncode=returncode, stdout=stdout, stderr=stderr
    )

# -- Fixture outputs per adapter ------------------------------------------------
# Each fixture returns a realistic AgentOutput as if the adapter ran successfully.


@pytest.fixture()
def claude_output() -> AgentOutput:
    return AgentOutput(
        stdout="Fixed the bug in main.py",
        stderr=None,
        exit_code=0,
        duration_seconds=12.5,
        input_tokens=1500,
        output_tokens=350,
        cache_read_tokens=200,
        cost_usd=0.0042,
        cost_model="per_token",
        cost_source="api_reported",
    )


@pytest.fixture()
def copilot_output() -> AgentOutput:
    return AgentOutput(
        stdout="Applied fix to utils.py",
        stderr=None,
        exit_code=0,
        duration_seconds=8.3,
        input_tokens=1200,
        output_tokens=280,
        cost_usd=0.0031,
        cost_model="per_token",
        cost_source="log_parsed",
    )


@pytest.fixture()
def codex_output() -> AgentOutput:
    return AgentOutput(
        stdout="Refactored the module",
        stderr=None,
        exit_code=0,
        duration_seconds=6.1,
        input_tokens=800,
        output_tokens=420,
        cost_usd=0.0025,
        cost_model="per_token",
        cost_source="api_reported",
    )


# -- Contract assertions -------------------------------------------------------

_ADAPTERS = ["claude", "copilot", "codex"]


@pytest.fixture(params=_ADAPTERS)
def adapter_output(request: pytest.FixtureRequest) -> AgentOutput:
    """Parametrized fixture returning each adapter's output."""
    return request.getfixturevalue(f"{request.param}_output")


class TestAdapterOutputContract:
    """Every adapter output must have non-None cost and token fields."""

    def test_cost_usd_not_none(self, adapter_output: AgentOutput) -> None:
        assert adapter_output.cost_usd is not None

    def test_cost_model_not_none(self, adapter_output: AgentOutput) -> None:
        assert adapter_output.cost_model is not None

    def test_cost_source_not_none(self, adapter_output: AgentOutput) -> None:
        assert adapter_output.cost_source is not None

    def test_input_tokens_not_none(self, adapter_output: AgentOutput) -> None:
        assert adapter_output.input_tokens is not None

    def test_output_tokens_not_none(self, adapter_output: AgentOutput) -> None:
        assert adapter_output.output_tokens is not None


# -- Real parse-path contract tests ---------------------------------------------


class TestClaudeParseContract:
    """ClaudeAdapter.parse_output against real CLI envelope transcripts."""

    def _adapter(self):
        from codeprobe.adapters.claude import ClaudeAdapter

        return ClaudeAdapter()

    def test_envelope_extracts_tokens_and_cost(self) -> None:
        raw = (FIXTURES / "claude_normal.json").read_text()
        out = self._adapter().parse_output(_completed(raw), duration=1.0)
        assert out.input_tokens == 12345
        assert out.output_tokens == 6789
        assert out.cache_read_tokens == 1000
        assert out.cache_creation_tokens == 500
        assert out.cost_usd == pytest.approx(0.0423)
        assert out.cost_model == "per_token"
        assert out.cost_source == "api_reported"
        assert out.error is None
        assert out.stdout == "Here is the fix for the bug..."

    def test_usage_less_envelope_is_honest(self) -> None:
        raw = (FIXTURES / "claude_no_usage.json").read_text()
        out = self._adapter().parse_output(_completed(raw), duration=1.0)
        assert out.error is not None
        assert out.cost_usd is None
        assert out.cost_model == "unknown"
        assert out.cost_source == "unavailable"

    def test_quota_stub_routes_to_quota_category(self) -> None:
        raw = "You've reached your monthly usage limit."
        out = self._adapter().parse_output(_completed(raw), duration=1.0)
        assert out.error_category == "quota"
        assert out.error is not None and "quota" in out.error.lower()


class TestCopilotParseContract:
    """CopilotAdapter.parse_output against real NDJSON transcripts."""

    def _adapter(self):
        from codeprobe.adapters.copilot import CopilotAdapter

        return CopilotAdapter()

    @patch("codeprobe.adapters.telemetry._count_tokens_tiktoken", return_value=None)
    def test_ndjson_extracts_tokens_and_estimated_cost(self, _mock) -> None:
        raw = (FIXTURES / "copilot_normal.txt").read_text()
        out = self._adapter().parse_output(_completed(raw), duration=1.0)
        assert out.output_tokens == 87
        assert out.input_tokens is not None and out.input_tokens > 0
        assert out.cost_usd is not None
        assert out.cost_model == "per_token"
        # Copilot CLI reports no usage dollars — the cost is heuristic and
        # must say so, never masquerade as api_reported.
        assert out.cost_source == "estimated"

    def test_token_less_ndjson_is_honest(self) -> None:
        raw = (FIXTURES / "copilot_no_tokens.txt").read_text()
        out = self._adapter().parse_output(_completed(raw), duration=1.0)
        assert out.error is not None
        assert out.cost_usd is None
        assert out.cost_source == "unavailable"


class TestCodexRunContract:
    """CodexAdapter.run with the OpenAI client stubbed at the seam.

    The usage-less path is the one the hand-built fixtures could never
    catch: an API response with ``usage=None`` must yield an AgentOutput
    that declares its missing telemetry instead of silently shipping
    ``cost_usd=None`` with no error.
    """

    def _run(self, response: object) -> AgentOutput:
        import openai

        from codeprobe.adapters.codex import CodexAdapter

        client = SimpleNamespace(
            responses=SimpleNamespace(create=lambda **kw: response)
        )
        with patch.object(openai, "OpenAI", return_value=client):
            return CodexAdapter().run("fix the bug", AgentConfig())

    def test_usage_extracts_calculated_cost(self) -> None:
        response = SimpleNamespace(
            output_text="Refactored the module",
            usage=SimpleNamespace(input_tokens=800, output_tokens=420),
        )
        out = self._run(response)
        assert out.input_tokens == 800
        assert out.output_tokens == 420
        # codex-mini-latest pricing: (1.50, 6.00) per 1M tokens
        assert out.cost_usd == pytest.approx(
            800 * 1.50 / 1_000_000 + 420 * 6.00 / 1_000_000
        )
        assert out.cost_model == "per_token"
        assert out.cost_source == "calculated"
        assert out.error is None

    def test_usage_less_response_is_honest(self) -> None:
        response = SimpleNamespace(output_text="done", usage=None)
        out = self._run(response)
        assert out.stdout == "done"
        assert out.input_tokens is None
        assert out.output_tokens is None
        assert out.cost_usd is None
        assert out.cost_model == "unknown"
        assert out.cost_source == "unavailable"
        # The honesty contract: missing vendor usage must be declared.
        assert out.error is not None and "usage" in out.error.lower()


# -- Registry lazy-import tests -------------------------------------------------


class TestRegistryLazyImport:
    """Importing codeprobe must not crash when optional CLI tools are missing."""

    def test_import_codeprobe_does_not_crash(self) -> None:
        """Importing the top-level package must always succeed."""
        import codeprobe  # noqa: F401

    def test_resolve_missing_adapter_gives_clear_error(self) -> None:
        """Resolving an unknown adapter should raise KeyError, not ImportError."""
        from codeprobe.core.registry import resolve

        with pytest.raises(KeyError, match="Unknown agent adapter"):
            resolve("nonexistent_adapter")
