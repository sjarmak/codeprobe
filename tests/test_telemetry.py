"""Tests for telemetry collectors — independent of adapters."""

from __future__ import annotations

from pathlib import Path

import pytest

from codeprobe.adapters.telemetry import (
    ApiResponseCollector,
    JsonStdoutCollector,
    NdjsonStreamCollector,
    TelemetryCollector,
    UsageData,
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

    def test_normal(self):
        raw = (FIXTURES / "copilot_normal.txt").read_text()
        usage = self.collector.collect(raw)
        assert usage.output_tokens == 87
        assert usage.cost_model == "subscription"
        assert usage.cost_source == "api_reported"
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
