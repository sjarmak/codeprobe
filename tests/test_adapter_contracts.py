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
        # No JSON envelope was parsed — result-record fields stay None
        # and the failure is not declared terminal (it must be retried).
        assert out.num_turns is None
        assert out.result_subtype is None
        assert out.error_terminal is False

    def test_envelope_extracts_result_record_fields(self) -> None:
        """num_turns / subtype / duration_api_ms persist for successes (codeprobe-8up)."""
        raw = (FIXTURES / "claude_normal.json").read_text()
        out = self._adapter().parse_output(_completed(raw), duration=1.0)
        assert out.num_turns == 42
        assert out.result_subtype == "success"
        assert out.duration_api_ms == 123456
        assert out.error is None

    def test_max_turns_envelope_preserves_result_record(self) -> None:
        """A cap-hit is an error envelope but keeps its result record."""
        raw = (FIXTURES / "claude_max_turns.json").read_text()
        out = self._adapter().parse_output(_completed(raw), duration=1.0)
        assert out.error is not None
        assert out.result_subtype == "error_max_turns"
        assert out.num_turns == 90
        assert out.duration_api_ms == 1854321
        # Mid-run failure: real token/cost data is preserved.
        assert out.input_tokens == 54321
        assert out.cost_usd == pytest.approx(6.99)
        assert out.cost_source == "api_reported"
        # A cap-hit is a terminal agent outcome — a genuine 0.0-reward
        # measurement, kept on checkpoint resume (codeprobe-8up).
        assert out.error_terminal is True

    def test_non_cap_error_subtype_not_declared_terminal(self) -> None:
        """Only positively-recognised stop conditions are terminal."""
        import json as _json

        envelope = _json.loads((FIXTURES / "claude_max_turns.json").read_text())
        envelope["subtype"] = "error_during_execution"
        out = self._adapter().parse_output(
            _completed(_json.dumps(envelope)), duration=1.0
        )
        assert out.error is not None
        assert out.error_terminal is False


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


# -- Capability declaration contract ---------------------------------------------


class TestAdapterCapabilities:
    """Every builtin adapter declares capabilities matching its config.* usage.

    These assertions are exact (``==`` on the frozen dataclass), so adding
    knob support to an adapter without updating its declaration — or vice
    versa — fails here (codeprobe-f7rl.26).
    """

    def test_every_builtin_exposes_capabilities(self) -> None:
        from codeprobe.adapters.protocol import AdapterCapabilities
        from codeprobe.core.registry import resolve

        for name in ("claude", "codex", "copilot"):
            adapter = resolve(name)
            assert isinstance(adapter.capabilities, AdapterCapabilities), name

    def test_claude_honors_every_knob(self) -> None:
        from codeprobe.adapters.claude import ClaudeAdapter
        from codeprobe.adapters.protocol import AdapterCapabilities

        assert ClaudeAdapter().capabilities == AdapterCapabilities(
            mcp_config=True,
            allowed_tools=True,
            disallowed_tools=True,
            max_turns=True,
            permission_mode=True,
            workspace_cwd=True,
            timeout=True,
        )

    def test_codex_honors_no_knobs(self) -> None:
        from codeprobe.adapters.codex import CodexAdapter
        from codeprobe.adapters.protocol import AdapterCapabilities

        assert CodexAdapter().capabilities == AdapterCapabilities()

    def test_copilot_honors_exactly_mcp_cwd_timeout(self) -> None:
        from codeprobe.adapters.copilot import CopilotAdapter
        from codeprobe.adapters.protocol import AdapterCapabilities

        assert CopilotAdapter().capabilities == AdapterCapabilities(
            mcp_config=True,
            workspace_cwd=True,
            timeout=True,
        )

    def test_openai_compat_honors_no_knobs(self) -> None:
        from codeprobe.adapters.openai_compat import OpenAICompatAdapter
        from codeprobe.adapters.protocol import AdapterCapabilities

        adapter = OpenAICompatAdapter(api_base="http://localhost:11434/v1", model="m")
        assert adapter.capabilities == AdapterCapabilities()

    def test_capabilities_of_is_fail_closed(self) -> None:
        """No declaration — or a wrong-typed one — means prompt+model only."""
        from codeprobe.adapters.protocol import AdapterCapabilities, capabilities_of

        class Undeclared:
            pass

        class WrongType:
            capabilities = {"mcp_config": True}

        assert capabilities_of(Undeclared()) == AdapterCapabilities()
        assert capabilities_of(WrongType()) == AdapterCapabilities()


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
