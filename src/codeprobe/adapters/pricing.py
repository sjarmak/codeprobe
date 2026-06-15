"""Vendor pricing tables — one per adapter, each with its own staleness clock.

Pricing is reference data, not parsing logic: it changes on vendor
announcements, while the telemetry collectors change on output-format
drift. Keeping the tables here means a new adapter adds its table without
touching the shared parser module, and re-verifying one vendor's prices
doesn't silently reset the staleness clock for every other vendor.

Adding an adapter: define a :class:`PricingTable` here with today's date
as ``last_verified`` after checking the vendor's pricing page. Update
``last_verified`` whenever the rates are re-checked — the per-table
staleness warning keys off it.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from datetime import date

#: Days after ``last_verified`` before a table is considered stale.
PRICING_STALENESS_DAYS = 90


@dataclass(frozen=True)
class PricingTable:
    """Per-vendor pricing with its own verification date.

    ``rates`` maps model name → per-1M-token rate tuple. The tuple shape
    is vendor-specific — ``(input, output)`` for most vendors,
    ``(input, output, cache_read, cache_creation)`` for Claude — and
    consumers know their vendor's shape. Look rates up directly on
    ``table.rates`` (a plain dict).
    """

    vendor: str
    rates: dict[str, tuple[float, ...]]
    last_verified: date


def staleness_warn(table: PricingTable) -> None:
    """Warn if *table* was last verified over ``PRICING_STALENESS_DAYS`` ago.

    No-op when the table is still fresh. Called once per table at import so a
    rate that has gone unchecked past the threshold surfaces a ``UserWarning``
    pointing at the file and date to re-verify.
    """
    age_days = (date.today() - table.last_verified).days
    if age_days > PRICING_STALENESS_DAYS:
        warnings.warn(
            f"{table.vendor} pricing was last verified on "
            f"{table.last_verified} ({age_days} days ago, threshold "
            f"{PRICING_STALENESS_DAYS}). Re-check the vendor pricing page "
            "and update last_verified in codeprobe/adapters/pricing.py.",
            UserWarning,
            stacklevel=1,
        )


# Pricing per 1M tokens: (input, output)
CODEX_PRICING = PricingTable(
    vendor="codex",
    rates={
        "codex-mini-latest": (1.50, 6.00),
        "codex-latest": (2.00, 8.00),
    },
    last_verified=date(2026, 4, 2),
)

# Claude pricing per 1M tokens: (input, output, cache_read, cache_creation)
# Cache creation is billed at 1.25x the input rate.
CLAUDE_PRICING = PricingTable(
    vendor="claude",
    rates={
        "claude-opus-4-6": (15.00, 75.00, 1.50, 18.75),
        "claude-sonnet-4-6": (3.00, 15.00, 0.30, 3.75),
        "claude-haiku-4-5": (0.80, 4.00, 0.08, 1.00),
    },
    last_verified=date(2026, 4, 2),
)

# Copilot pricing per 1M tokens: (input, output)
# GPT-4o rates — Copilot's underlying model for code tasks.
COPILOT_PRICING = PricingTable(
    vendor="copilot",
    rates={
        "gpt-4o": (2.50, 10.00),
        "gpt-4o-mini": (0.15, 0.60),
    },
    last_verified=date(2026, 4, 2),
)

ALL_PRICING_TABLES: tuple[PricingTable, ...] = (
    CODEX_PRICING,
    CLAUDE_PRICING,
    COPILOT_PRICING,
)

for _table in ALL_PRICING_TABLES:
    staleness_warn(_table)
