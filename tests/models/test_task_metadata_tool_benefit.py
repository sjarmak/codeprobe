"""Tests for the tool-benefit + capability-snapshot fields on TaskMetadata.

Bead: r4-tool-benefit-metadata. Acceptance criteria 1, 6 tracked here.
"""

from __future__ import annotations

from dataclasses import asdict

import pytest

from codeprobe.models.task import Task, TaskMetadata


class TestDefaults:
    """The three new fields must have empty defaults so existing call sites
    (which pass positional/kwarg construction without these keys) keep
    working unchanged."""

    def test_expected_tool_benefit_default_is_empty_string(self) -> None:
        m = TaskMetadata(name="t")
        assert m.expected_tool_benefit == ""

    def test_tool_benefit_rationale_default_is_empty_string(self) -> None:
        m = TaskMetadata(name="t")
        assert m.tool_benefit_rationale == ""

    def test_capability_snapshot_default_is_empty_tuple(self) -> None:
        m = TaskMetadata(name="t")
        assert m.mcp_capabilities_at_mine_time == ()


class TestAcceptedLevels:
    """Level values are not enforced at the dataclass level (it's just a
    string), but the contract is '' | 'low' | 'medium' | 'high'. Confirm
    all four round-trip through construction + asdict."""

    @pytest.mark.parametrize("level", ["", "low", "medium", "high"])
    def test_round_trip(self, level: str) -> None:
        m = TaskMetadata(name="t", expected_tool_benefit=level)
        assert m.expected_tool_benefit == level
        dumped = asdict(m)
        assert dumped["expected_tool_benefit"] == level


class TestHashability:
    """TaskMetadata is a frozen dataclass used in sets (see existing
    test_task_model.py). New fields must not break hashability — hence
    ``tuple[str, ...]`` rather than ``list[str]`` for the capability
    snapshot."""

    def test_metadata_with_capability_snapshot_is_hashable(self) -> None:
        m = TaskMetadata(
            name="t",
            mcp_capabilities_at_mine_time=("KEYWORD_SEARCH", "FILE_READ"),
        )
        assert isinstance(hash(m), int)
        {m}  # noqa: B018 — assert set-insertable

    def test_task_with_capability_snapshot_is_hashable(self) -> None:
        t = Task(
            id="t1",
            repo="r",
            metadata=TaskMetadata(
                name="t",
                expected_tool_benefit="medium",
                tool_benefit_rationale="Would benefit from keyword search.",
                mcp_capabilities_at_mine_time=("KEYWORD_SEARCH",),
            ),
        )
        assert isinstance(hash(t), int)


class TestAsdictSerialization:
    """asdict must surface all three new fields so writer.py's
    ``json.dumps(asdict(task))`` emits them into metadata.json."""

    def test_asdict_includes_all_three_new_fields(self) -> None:
        m = TaskMetadata(
            name="t",
            expected_tool_benefit="high",
            tool_benefit_rationale="Cross-file refactor with many references.",
            mcp_capabilities_at_mine_time=(
                "FILE_READ",
                "GO_TO_DEFINITION",
                "KEYWORD_SEARCH",
            ),
        )
        dumped = asdict(m)
        assert dumped["expected_tool_benefit"] == "high"
        assert (
            dumped["tool_benefit_rationale"]
            == "Cross-file refactor with many references."
        )
        assert dumped["mcp_capabilities_at_mine_time"] == (
            "FILE_READ",
            "GO_TO_DEFINITION",
            "KEYWORD_SEARCH",
        )


class TestNoLegacyField:
    """Regression: the previous design used ``expected_mcp_benefit`` —
    confirm it is NOT a field on TaskMetadata (acceptance criterion 2).
    """

    def test_expected_mcp_benefit_attribute_absent(self) -> None:
        m = TaskMetadata(name="t")
        assert not hasattr(m, "expected_mcp_benefit")
