"""Tests for task-category-aware turn-cap resolution (codeprobe-gg9f)."""

from __future__ import annotations

import pytest

from codeprobe.core.turn_cap import (
    DEFAULT_FAMILY_MAX_TURNS,
    FAMILY_DEFAULT_MAX_TURNS,
    TurnCapDecision,
    resolve_turn_cap,
    resolve_turn_cap_family,
)

# ---------------------------------------------------------------------------
# Family resolution
# ---------------------------------------------------------------------------


class TestResolveFamily:
    """resolve_turn_cap_family maps explicit metadata fields to a family key."""

    def test_oracle_checks_from_reward_type(self) -> None:
        family = resolve_turn_cap_family(
            {"category": "sdlc"}, {"reward_type": "oracle_checks"}
        )
        # verification signal wins over the category block.
        assert family == "oracle_checks"

    def test_oracle_checks_from_verification_type(self) -> None:
        family = resolve_turn_cap_family({}, {"type": "oracle_checks"})
        assert family == "oracle_checks"

    def test_sdlc_from_category(self) -> None:
        family = resolve_turn_cap_family({"category": "sdlc"}, {})
        assert family == "sdlc"

    def test_other_category_passed_through(self) -> None:
        family = resolve_turn_cap_family({"category": "micro_probe"}, {})
        assert family == "micro_probe"

    def test_empty_when_no_signal(self) -> None:
        assert resolve_turn_cap_family({}, {}) == ""

    def test_non_string_category_ignored(self) -> None:
        assert resolve_turn_cap_family({"category": 123}, {}) == ""


# ---------------------------------------------------------------------------
# Precedence ladder
# ---------------------------------------------------------------------------


class TestPrecedence:
    """CLI / experiment > task override > family default > global fallback."""

    def test_cli_wins_over_everything(self) -> None:
        decision = resolve_turn_cap(
            config_max_turns=42,
            config_source="cli",
            task_override=30,
            family="oracle_checks",
        )
        assert decision == TurnCapDecision(42, "cli")

    def test_experiment_config_wins_over_task(self) -> None:
        decision = resolve_turn_cap(
            config_max_turns=100,
            config_source="experiment",
            task_override=30,
            family="sdlc",
        )
        assert decision == TurnCapDecision(100, "experiment")

    def test_config_source_defaults_to_experiment(self) -> None:
        # A cap with no source label is treated as an experiment-config cap.
        decision = resolve_turn_cap(
            config_max_turns=55,
            config_source="",
            task_override=None,
            family="sdlc",
        )
        assert decision == TurnCapDecision(55, "experiment")

    def test_task_override_wins_over_family(self) -> None:
        decision = resolve_turn_cap(
            config_max_turns=None,
            config_source="",
            task_override=30,
            family="oracle_checks",
        )
        assert decision == TurnCapDecision(30, "task")

    def test_family_default_oracle_checks(self) -> None:
        decision = resolve_turn_cap(
            config_max_turns=None,
            config_source="",
            task_override=None,
            family="oracle_checks",
        )
        assert decision == TurnCapDecision(50, "family_default")

    def test_family_default_sdlc_is_uncapped(self) -> None:
        decision = resolve_turn_cap(
            config_max_turns=None,
            config_source="",
            task_override=None,
            family="sdlc",
        )
        assert decision == TurnCapDecision(None, "family_default")

    def test_unknown_family_falls_back_to_default(self) -> None:
        decision = resolve_turn_cap(
            config_max_turns=None,
            config_source="",
            task_override=None,
            family="micro_probe",
        )
        assert decision == TurnCapDecision(DEFAULT_FAMILY_MAX_TURNS, "family_default")

    def test_no_family_falls_back_to_default(self) -> None:
        decision = resolve_turn_cap(
            config_max_turns=None,
            config_source="",
            task_override=None,
            family="",
        )
        assert decision == TurnCapDecision(DEFAULT_FAMILY_MAX_TURNS, "family_default")


# ---------------------------------------------------------------------------
# Table invariants
# ---------------------------------------------------------------------------


def test_family_table_contract() -> None:
    """The decided defaults: sdlc uncapped, oracle_checks 50, fallback 75."""
    assert FAMILY_DEFAULT_MAX_TURNS["sdlc"] is None
    assert FAMILY_DEFAULT_MAX_TURNS["oracle_checks"] == 50
    assert DEFAULT_FAMILY_MAX_TURNS == 75


@pytest.mark.parametrize("cap", [None, 50, 75])
def test_decision_is_frozen(cap: int | None) -> None:
    decision = TurnCapDecision(cap, "family_default")
    with pytest.raises(Exception):
        decision.max_turns = 1  # type: ignore[misc]
