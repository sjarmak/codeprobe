"""Tests for Phase 2 task family definitions."""

from __future__ import annotations

import re

from codeprobe.mining.org_scale_families import (
    ALL_FAMILIES,
    CROSS_REPO_CONFIG_TRACE,
    FAMILIES,
    FAMILY_BY_NAME,
    INCIDENT_DEBUG,
    MCP_FAMILIES,
    PLATFORM_KNOWLEDGE,
)


class TestFamilyCounts:
    def test_families_tuple_has_six_entries(self) -> None:
        assert len(FAMILIES) == 6

    def test_family_by_name_has_all_keys(self) -> None:
        assert len(FAMILY_BY_NAME) == 9  # 6 base + 3 MCP-advantaged

    def test_all_expected_names_present(self) -> None:
        expected = {
            "migration-inventory",
            "compliance-audit",
            "cross-repo-dep-trace",
            "incident-debug",
            "platform-knowledge",
            "cross-repo-config-trace",
            "symbol-reference-trace",
            "type-hierarchy-consumers",
            "change-scope-audit",
        }
        assert set(FAMILY_BY_NAME.keys()) == expected


class TestRequiredFields:
    """Every family must have non-empty required fields."""

    def test_all_families_have_required_fields(self) -> None:
        for family in FAMILIES:
            assert family.name, f"{family} missing name"
            assert family.description, f"{family.name} missing description"
            assert family.glob_patterns, f"{family.name} missing glob_patterns"
            assert family.content_patterns, f"{family.name} missing content_patterns"
            assert family.oracle_type, f"{family.name} missing oracle_type"

    def test_new_families_oracle_type_is_file_list(self) -> None:
        for family in (INCIDENT_DEBUG, PLATFORM_KNOWLEDGE, CROSS_REPO_CONFIG_TRACE):
            assert (
                family.oracle_type == "file_list"
            ), f"{family.name} oracle_type should be 'file_list'"

    def test_new_families_are_multi_hop(self) -> None:
        for family in (INCIDENT_DEBUG, PLATFORM_KNOWLEDGE, CROSS_REPO_CONFIG_TRACE):
            assert family.multi_hop is True, f"{family.name} should be multi_hop"
            assert (
                family.multi_hop_description
            ), f"{family.name} missing multi_hop_description"


class TestPatternCompilability:
    """All content_patterns must be valid regex."""

    def test_all_content_patterns_compile(self) -> None:
        for family in ALL_FAMILIES:
            for pattern in family.content_patterns:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise AssertionError(
                        f"{family.name} pattern {pattern!r} failed to compile: {exc}"
                    ) from exc


class TestIncidentDebug:
    def test_name(self) -> None:
        assert INCIDENT_DEBUG.name == "incident-debug"

    def test_has_error_patterns(self) -> None:
        joined = " ".join(INCIDENT_DEBUG.content_patterns)
        assert "Error" in joined
        assert "panic" in joined
        assert "raise" in joined


class TestPlatformKnowledge:
    def test_name(self) -> None:
        assert PLATFORM_KNOWLEDGE.name == "platform-knowledge"

    def test_has_plugin_patterns(self) -> None:
        joined = " ".join(PLATFORM_KNOWLEDGE.content_patterns)
        assert "Register" in joined
        assert "Plugin" in joined
        assert "extension_point" in joined


class TestCrossRepoConfigTrace:
    def test_name(self) -> None:
        assert CROSS_REPO_CONFIG_TRACE.name == "cross-repo-config-trace"

    def test_has_config_patterns(self) -> None:
        joined = " ".join(CROSS_REPO_CONFIG_TRACE.content_patterns)
        assert "Config" in joined
        assert "viper" in joined
        assert "environ" in joined


class TestMCPFamilies:
    def test_mcp_families_has_three_entries(self) -> None:
        assert len(MCP_FAMILIES) == 3

    def test_mcp_families_are_multi_hop(self) -> None:
        for family in MCP_FAMILIES:
            assert family.multi_hop is True, f"{family.name} should be multi_hop"
            assert family.multi_hop_description

    def test_mcp_families_have_required_fields(self) -> None:
        for family in MCP_FAMILIES:
            assert family.name
            assert family.description
            assert family.glob_patterns
            assert family.content_patterns
            assert family.oracle_type == "file_list"

    def test_all_families_equals_families_plus_mcp(self) -> None:
        assert ALL_FAMILIES == FAMILIES + MCP_FAMILIES
