"""Tests for the vendor pricing tables in codeprobe.adapters.pricing."""

from __future__ import annotations

import warnings
from datetime import date, timedelta

import pytest

from codeprobe.adapters.pricing import (
    CLAUDE_PRICING,
    PRICING_STALENESS_DAYS,
    PricingTable,
    staleness_warn,
)


def test_claude_pricing_current_rates() -> None:
    """Rates verified 2026-07-17 against platform.claude.com/docs pricing page."""
    assert CLAUDE_PRICING.rates["claude-sonnet-4-6"] == (3.00, 15.00, 0.30, 3.75)
    assert CLAUDE_PRICING.rates["claude-haiku-4-5"] == (1.00, 5.00, 0.10, 1.25)
    assert CLAUDE_PRICING.rates["claude-opus-4-6"] == (5.00, 25.00, 0.50, 6.25)


def test_claude_pricing_cache_creation_is_1_25x_input() -> None:
    for model, (input_rate, _output, _cache_read, cache_creation) in CLAUDE_PRICING.rates.items():
        assert cache_creation == pytest.approx(input_rate * 1.25), model


def test_claude_pricing_not_stale() -> None:
    age_days = (date.today() - CLAUDE_PRICING.last_verified).days
    assert age_days <= PRICING_STALENESS_DAYS


def test_staleness_warn_fires_past_threshold() -> None:
    stale_table = PricingTable(
        vendor="test-vendor",
        rates={"m": (1.0, 2.0)},
        last_verified=date.today() - timedelta(days=PRICING_STALENESS_DAYS + 1),
    )
    with pytest.warns(UserWarning, match="test-vendor pricing was last verified"):
        staleness_warn(stale_table)


def test_staleness_warn_silent_within_threshold() -> None:
    fresh_table = PricingTable(
        vendor="test-vendor",
        rates={"m": (1.0, 2.0)},
        last_verified=date.today(),
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        staleness_warn(fresh_table)
