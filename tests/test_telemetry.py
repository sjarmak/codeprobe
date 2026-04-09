"""Tests for telemetry collectors — independent of adapters."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unittest.mock import MagicMock, patch

from codeprobe.adapters.telemetry import (
    COPILOT_PRICING,
    ApiResponseCollector,
    JsonStdoutCollector,
    NdjsonStreamCollector,
    TelemetryCollector,
    UsageData,
    _PRICING_LAST_VERIFIED,
    _count_tokens_tiktoken,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# -- UsageData model tests ----------------------------------------------------


def test_usage_data_is_frozen():
    usage = UsageData(input_tokens=100)
    with pytest.raises(AttributeError):
        usage.input_tokens = 200  # type: ignore[misc]


def test_usage_data_validates_cost_model():
    with pytest.raises(ValueError, match="cost_model"):
        UsageData(cost_model="bogus")


def test_usage_data_validates_cost_source():
    with pytest.raises(ValueError, match="cost_source"):
        UsageData(cost_source="bogus")


def test_usage_data_defaults():
    usage = UsageData()
    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.cache_read_tokens is None
    assert usage.cost_usd is None
    assert usage.cost_model == "unknown"
    assert usage.cost_source == "unavailable"
    assert usage.error is None
    assert usage.tool_call_count is None


def test_usage_data_tool_call_count():
    usage = UsageData(tool_call_count=5)
    assert usage.tool_call_count == 5


# -- Protocol conformance -----------------------------------------------------


def test_collectors_implement_protocol():
    assert isinstance(JsonStdoutCollector(), TelemetryCollector)
    assert isinstance(NdjsonStreamCollector(), TelemetryCollector)
    assert isinstance(ApiResponseCollector(), TelemetryCollector)


# -- JsonStdoutCollector tests ------------------------------------------------


class TestJsonStdoutCollector:
    collector = JsonStdoutCollector()

    def test_normal(self):
        raw = (FIXTURES / "claude_normal.json").read_text()
        usage = self.collector.collect(raw)
        assert usage.input_tokens == 12345
        assert usage.output_tokens == 6789
        assert usage.cache_read_tokens == 1000
        assert usage.cost_usd == 0.0423
        assert usage.cost_model == "per_token"
        assert usage.cost_source == "api_reported"
        assert usage.error is None

    def test_no_usage(self):
        raw = (FIXTURES / "claude_no_usage.json").read_text()
        usage = self.collector.collect(raw)
        assert usage.error is not None
        assert "Missing usage" in usage.error
        assert usage.input_tokens is None

    def test_partial_usage(self):
        raw = (FIXTURES / "claude_partial_usage.json").read_text()
        usage = self.collector.collect(raw)
        assert usage.input_tokens is not None
        assert usage.output_tokens is None  # not in partial fixture
        assert usage.error is None

    def test_usage_present_but_no_cost(self):
        """Usage block present but no total_cost_usd → cost_model=unknown."""
        raw = '{"result": "ok", "usage": {"input_tokens": 100, "output_tokens": 50}}'
        usage = self.collector.collect(raw)
        assert usage.input_tokens == 100
        assert usage.output_tokens == 50
        assert usage.cost_usd is None
        assert usage.cost_model == "unknown"
        assert usage.cost_source == "unavailable"
        assert usage.error is None

    def test_tool_call_count_from_messages(self):
        """Count tool_use content blocks in the messages array."""
        raw = (FIXTURES / "claude_with_tools.json").read_text()
        usage = self.collector.collect(raw)
        assert usage.tool_call_count == 3
        assert usage.error is None

    def test_tool_call_count_none_without_messages(self):
        """When no messages array is present, tool_call_count is None."""
        raw = (FIXTURES / "claude_normal.json").read_text()
        usage = self.collector.collect(raw)
        assert usage.tool_call_count is None

    def test_tool_call_count_zero_no_tool_use(self):
        """Messages present but no tool_use blocks → count is 0."""
        raw = json.dumps(
            {
                "result": "done",
                "usage": {"input_tokens": 100, "output_tokens": 50},
                "total_cost_usd": 0.01,
                "messages": [
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
                ],
            }
        )
        usage = self.collector.collect(raw)
        assert usage.tool_call_count == 0

    def test_malformed_json(self):
        raw = (FIXTURES / "claude_malformed.json").read_text()
        usage = self.collector.collect(raw)
        assert usage.error is not None
        assert "JSON parse failed" in usage.error

    def test_empty_string(self):
        raw = (FIXTURES / "claude_empty.json").read_text()
        usage = self.collector.collect(raw)
        assert usage.error is not None


# -- NdjsonStreamCollector tests -----------------------------------------------


class TestNdjsonStreamCollector:
    collector = NdjsonStreamCollector()

    @patch("codeprobe.adapters.telemetry._count_tokens_tiktoken", return_value=None)
    def test_normal(self, _mock_tiktoken):
        raw = (FIXTURES / "copilot_normal.txt").read_text()
        usage = self.collector.collect(raw)
        assert usage.output_tokens == 87
        assert usage.input_tokens is not None
        assert usage.input_tokens > 0
        assert usage.cost_model == "per_token"
        assert usage.cost_source == "estimated"
        gpt4o = COPILOT_PRICING["gpt-4o"]
        expected_cost = (
            usage.input_tokens * gpt4o[0] / 1_000_000 + 87 * gpt4o[1] / 1_000_000
        )
        assert usage.cost_usd == pytest.approx(expected_cost, abs=1e-8)
        assert usage.error is None

    def test_no_tokens(self):
        raw = (FIXTURES / "copilot_no_tokens.txt").read_text()
        usage = self.collector.collect(raw)
        assert usage.error is not None
        assert "outputTokens" in usage.error

    def test_plain_text(self):
        raw = (FIXTURES / "copilot_plain.txt").read_text()
        usage = self.collector.collect(raw)
        assert usage.error is not None
        assert "structured JSON" in usage.error

    def test_empty_string(self):
        raw = (FIXTURES / "copilot_empty.txt").read_text()
        usage = self.collector.collect(raw)
        assert usage.error is not None

    def test_long_output(self):
        raw = (FIXTURES / "copilot_long.txt").read_text()
        usage = self.collector.collect(raw)
        assert usage.output_tokens == 312
        assert usage.error is None


# -- ApiResponseCollector tests ------------------------------------------------


class TestApiResponseCollector:
    collector = ApiResponseCollector()

    def test_with_usage_known_model(self):
        usage = self.collector.collect(
            "",
            input_tokens=1000,
            output_tokens=500,
            model="codex-mini-latest",
        )
        assert usage.input_tokens == 1000
        assert usage.output_tokens == 500
        expected_cost = 1000 * 1.50 / 1_000_000 + 500 * 6.00 / 1_000_000
        assert usage.cost_usd == pytest.approx(expected_cost)
        assert usage.cost_model == "per_token"
        assert usage.cost_source == "calculated"
        assert usage.error is None

    def test_with_usage_unknown_model(self):
        usage = self.collector.collect(
            "",
            input_tokens=1000,
            output_tokens=500,
            model="unknown-model",
        )
        assert usage.input_tokens == 1000
        assert usage.output_tokens == 500
        assert usage.cost_usd is None
        assert usage.cost_model == "unknown"
        assert usage.cost_source == "unavailable"
        assert usage.error is None

    def test_no_usage(self):
        usage = self.collector.collect("")
        assert usage.error is not None
        assert "no usage data" in usage.error

    def test_partial_usage_missing_output(self):
        usage = self.collector.collect("", input_tokens=100)
        assert usage.error is not None

    def test_codex_latest_pricing(self):
        usage = self.collector.collect(
            "",
            input_tokens=1000,
            output_tokens=500,
            model="codex-latest",
        )
        assert usage.input_tokens == 1000
        assert usage.output_tokens == 500
        # codex-latest: $2.00/1M input + $8.00/1M output
        expected_cost = 1000 * 2.00 / 1_000_000 + 500 * 8.00 / 1_000_000
        assert usage.cost_usd == pytest.approx(expected_cost)
        assert usage.cost_model == "per_token"
        assert usage.cost_source == "calculated"
        assert usage.error is None

    def test_custom_pricing(self):
        custom = ApiResponseCollector(pricing={"my-model": (2.0, 8.0)})
        usage = custom.collect(
            "",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            model="my-model",
        )
        assert usage.cost_usd == pytest.approx(10.0)


# -- Pricing metadata tests ---------------------------------------------------


class TestPricingMetadata:
    def test_copilot_pricing_has_gpt4o(self):
        assert "gpt-4o" in COPILOT_PRICING
        inp, out = COPILOT_PRICING["gpt-4o"]
        assert inp > 0
        assert out > 0

    def test_pricing_last_verified_is_date(self):
        from datetime import date

        assert isinstance(_PRICING_LAST_VERIFIED, date)

    def test_pricing_staleness_constants(self):
        """Verify staleness detection constants exist and are sensible."""
        from codeprobe.adapters.telemetry import _PRICING_STALENESS_DAYS

        assert _PRICING_STALENESS_DAYS == 90
        # The verified date should not be unreasonably far in the past
        from datetime import date as _date

        age = (_date.today() - _PRICING_LAST_VERIFIED).days
        assert age >= 0, "Verified date is in the future"


GOLDEN = Path(__file__).resolve().parent / "fixtures" / "golden"


class TestGoldenFileContracts:
    """Parse golden fixture files through collectors and assert exact field values.

    If an upstream CLI changes its output format, these tests fail with a clear
    message identifying which field changed.
    """

    def test_claude_golden_contract(self) -> None:
        raw = (GOLDEN / "claude_json_output.json").read_text()
        usage = JsonStdoutCollector().collect(raw)

        assert usage.error is None, f"Unexpected error: {usage.error}"

        assert (
            usage.input_tokens == 48231
        ), f"Golden contract broken: expected input_tokens=48231, got {usage.input_tokens}"
        assert (
            usage.output_tokens == 3847
        ), f"Golden contract broken: expected output_tokens=3847, got {usage.output_tokens}"
        assert (
            usage.cache_read_tokens == 12500
        ), f"Golden contract broken: expected cache_read_tokens=12500, got {usage.cache_read_tokens}"
        assert (
            usage.cost_usd == 0.1542
        ), f"Golden contract broken: expected cost_usd=0.1542, got {usage.cost_usd}"
        assert (
            usage.cost_source == "api_reported"
        ), f"Golden contract broken: expected cost_source='api_reported', got {usage.cost_source!r}"
        assert (
            usage.cost_model == "per_token"
        ), f"Golden contract broken: expected cost_model='per_token', got {usage.cost_model!r}"

    def test_copilot_golden_contract(self) -> None:
        raw = (GOLDEN / "copilot_ndjson_output.txt").read_text()
        usage = NdjsonStreamCollector().collect(raw)

        assert usage.error is None, f"Unexpected error: {usage.error}"

        # Copilot NDJSON usage event reports inputTokens=9200
        assert (
            usage.input_tokens == 9200
        ), f"Golden contract broken: expected input_tokens=9200, got {usage.input_tokens}"
        # output_tokens from assistant.message events: 156 + 42 = 198
        assert (
            usage.output_tokens == 198
        ), f"Golden contract broken: expected output_tokens=198, got {usage.output_tokens}"
        assert (
            usage.cost_source == "estimated"
        ), f"Golden contract broken: expected cost_source='estimated', got {usage.cost_source!r}"
        assert (
            usage.cost_model == "per_token"
        ), f"Golden contract broken: expected cost_model='per_token', got {usage.cost_model!r}"
        # Verify cost is computed from GPT-4o pricing
        gpt4o = COPILOT_PRICING["gpt-4o"]
        expected_cost = 9200 * gpt4o[0] / 1_000_000 + 198 * gpt4o[1] / 1_000_000
        assert usage.cost_usd == pytest.approx(
            expected_cost, abs=1e-8
        ), f"Golden contract broken: expected cost_usd={expected_cost}, got {usage.cost_usd}"


class TestNdjsonCostSourceEstimated:
    """Verify NdjsonStreamCollector uses 'estimated' cost_source."""

    collector = NdjsonStreamCollector()

    @patch("codeprobe.adapters.telemetry._count_tokens_tiktoken", return_value=None)
    def test_estimated_cost_source_with_heuristic_input(self, _mock_tiktoken):
        """When input_tokens come from _estimate_tokens(), cost_source must be 'estimated'."""
        raw = (
            '{"type":"user.message","data":{"content":"hello world"}}\n'
            '{"type":"assistant.message","data":{"content":"hi","outputTokens":10}}\n'
        )
        usage = self.collector.collect(raw)
        assert usage.cost_source == "estimated"
        assert usage.cost_model == "per_token"

    @patch("codeprobe.adapters.telemetry._count_tokens_tiktoken", return_value=None)
    def test_uses_copilot_pricing_not_claude(self, _mock_tiktoken):
        """Cost should be computed from COPILOT_PRICING, not CLAUDE_PRICING."""
        raw = (
            '{"type":"user.message","data":{"content":"test"}}\n'
            '{"type":"assistant.message","data":{"content":"resp","outputTokens":100}}\n'
        )
        usage = self.collector.collect(raw)
        gpt4o = COPILOT_PRICING["gpt-4o"]
        assert usage.input_tokens is not None
        expected = (
            usage.input_tokens * gpt4o[0] / 1_000_000 + 100 * gpt4o[1] / 1_000_000
        )
        assert usage.cost_usd == pytest.approx(expected, abs=1e-10)


# -- tiktoken integration tests ------------------------------------------------


class TestCountTokensTiktoken:
    """Unit tests for _count_tokens_tiktoken."""

    def test_returns_none_when_tiktoken_not_installed(self):
        """Without tiktoken, the function returns None."""
        with patch(
            "codeprobe.adapters.telemetry.tiktoken",
            new=None,
            create=True,
        ):
            # Simulate ImportError by patching the import mechanism
            import importlib
            import codeprobe.adapters.telemetry as tel_mod

            original = tel_mod._count_tokens_tiktoken

            # The function tries `import tiktoken` internally, so we need to
            # make that import fail.
            import builtins

            real_import = builtins.__import__

            def mock_import(name, *args, **kwargs):
                if name == "tiktoken":
                    raise ImportError("no tiktoken")
                return real_import(name, *args, **kwargs)

            with patch.object(builtins, "__import__", side_effect=mock_import):
                result = _count_tokens_tiktoken("hello world", "gpt-4o")

            assert result is None

    def test_returns_token_count_when_tiktoken_available(self):
        """With tiktoken mocked, returns the token count from encode()."""
        mock_enc = MagicMock()
        mock_enc.encode.return_value = [1, 2, 3, 4, 5]  # 5 tokens

        mock_tiktoken = MagicMock()
        mock_tiktoken.encoding_for_model.return_value = mock_enc

        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "tiktoken":
                return mock_tiktoken
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=mock_import):
            result = _count_tokens_tiktoken("hello world test", "gpt-4o")

        assert result == 5
        mock_tiktoken.encoding_for_model.assert_called_once_with("gpt-4o")
        mock_enc.encode.assert_called_once_with("hello world test")


class TestNdjsonTiktokenIntegration:
    """Verify NdjsonStreamCollector uses tiktoken when available."""

    _NDJSON_INPUT = (
        '{"type":"user.message","data":{"content":"hello world"}}\n'
        '{"type":"assistant.message","data":{"content":"hi there","outputTokens":10}}\n'
    )

    def test_tiktoken_available_sets_calculated(self):
        """When tiktoken succeeds, cost_source should be 'calculated'."""
        mock_enc = MagicMock()
        # Return a specific token list so we can verify exact input_tokens
        mock_enc.encode.return_value = list(range(7))  # 7 tokens

        mock_tiktoken = MagicMock()
        mock_tiktoken.encoding_for_model.return_value = mock_enc

        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "tiktoken":
                return mock_tiktoken
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=mock_import):
            usage = NdjsonStreamCollector().collect(self._NDJSON_INPUT)

        assert usage.input_tokens == 7
        assert usage.cost_source == "calculated"
        assert usage.cost_model == "per_token"
        assert usage.error is None

        # Verify cost is computed from exact token count
        gpt4o = COPILOT_PRICING["gpt-4o"]
        expected_cost = 7 * gpt4o[0] / 1_000_000 + 10 * gpt4o[1] / 1_000_000
        assert usage.cost_usd == pytest.approx(expected_cost, abs=1e-10)

    def test_tiktoken_unavailable_falls_back_to_estimated(self):
        """When tiktoken is not installed, falls back to heuristic with 'estimated'."""
        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "tiktoken":
                raise ImportError("no tiktoken")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=mock_import):
            usage = NdjsonStreamCollector().collect(self._NDJSON_INPUT)

        assert usage.input_tokens is not None
        assert usage.input_tokens > 0
        assert usage.cost_source == "estimated"
        assert usage.cost_model == "per_token"
        assert usage.error is None
