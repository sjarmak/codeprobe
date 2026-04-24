"""Tests for the MCP capability registry, preamble renderer, and fixture server."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from codeprobe.mcp import (
    CAPABILITIES,
    CAPABILITIES_VERSION,
    Capability,
    get_capability,
    list_capabilities,
)
from codeprobe.mcp.capabilities import (
    FILE_READ,
    GO_TO_DEFINITION,
    KEYWORD_SEARCH,
    SYMBOL_REFERENCES,
)
from codeprobe.mcp.fixtures import list_tools, tools_list_response
from codeprobe.preambles.templates import list_templates, render

REQUIRED_IDS = (KEYWORD_SEARCH, SYMBOL_REFERENCES, FILE_READ, GO_TO_DEFINITION)


# ---------------------------------------------------------------------------
# Capability registry


def test_capabilities_registry_has_required_keys() -> None:
    for cap_id in REQUIRED_IDS:
        assert cap_id in CAPABILITIES, f"Missing required capability id: {cap_id}"


def test_capability_dataclass_shape() -> None:
    for cap in CAPABILITIES.values():
        assert isinstance(cap, Capability)
        assert isinstance(cap.id, str) and cap.id
        assert isinstance(cap.name, str) and cap.name
        assert isinstance(cap.description, str) and cap.description
        # input_schema is a mapping with at least a "type" or "properties" key
        assert hasattr(cap.input_schema, "__getitem__")
        assert "type" in cap.input_schema or "properties" in cap.input_schema


def test_capability_ids_match_mapping_keys() -> None:
    for key, cap in CAPABILITIES.items():
        assert key == cap.id


def test_get_capability_positive_lookup() -> None:
    cap = get_capability(KEYWORD_SEARCH)
    assert cap.id == KEYWORD_SEARCH
    assert cap.name == "keyword_search"


def test_get_capability_missing_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        get_capability("DOES_NOT_EXIST")


def test_list_capabilities_is_deterministic() -> None:
    first = list_capabilities()
    second = list_capabilities()
    assert first == second
    # Covers all required ids
    ids = {cap.id for cap in first}
    for cap_id in REQUIRED_IDS:
        assert cap_id in ids


def test_capabilities_version_is_semver_like() -> None:
    parts = CAPABILITIES_VERSION.split(".")
    assert len(parts) == 3
    for part in parts:
        assert part.isdigit()


# ---------------------------------------------------------------------------
# Preamble render


def test_mcp_base_template_is_registered() -> None:
    assert "mcp_base.md.j2" in list_templates()


def test_preamble_renders_from_capabilities() -> None:
    body = render("mcp_base.md.j2")
    # All four required capability names appear in the rendered body.
    for cap in list_capabilities():
        assert cap.name in body, f"Capability name missing from preamble: {cap.name}"
        # First sentence of the description should appear as well.
        snippet = cap.description.split(".")[0]
        assert snippet in body


def test_preamble_renames_when_capability_renamed() -> None:
    """Proves the template is driven by capability data, not hardcoded strings."""
    original = get_capability(KEYWORD_SEARCH)
    renamed = replace(original, name="totally_renamed_search", description="Stub description.")
    caps = [renamed] + [
        cap for cap in list_capabilities() if cap.id != KEYWORD_SEARCH
    ]

    body = render("mcp_base.md.j2", capabilities=caps)

    assert "totally_renamed_search" in body
    assert "Stub description" in body
    # Original name must NOT appear when we swap in the rename.
    assert "keyword_search" not in body


def test_preamble_render_empty_capabilities() -> None:
    """Rendering with no capabilities produces a body without capability sections."""
    body = render("mcp_base.md.j2", capabilities=[])
    # Base heading should still be present.
    assert "MCP Capabilities" in body
    # None of the real capability names should be present.
    for cap in list_capabilities():
        assert cap.name not in body


# ---------------------------------------------------------------------------
# Fixture server


def test_fixture_list_tools_schema_shape() -> None:
    tools = list_tools()
    assert isinstance(tools, list)
    assert len(tools) == len(list_capabilities())

    seen_names: set[str] = set()
    for tool in tools:
        assert isinstance(tool, dict)
        assert set(tool.keys()) >= {"name", "description", "inputSchema"}
        assert isinstance(tool["name"], str) and tool["name"]
        assert isinstance(tool["description"], str) and tool["description"]
        assert isinstance(tool["inputSchema"], dict)
        # MCP inputSchema convention: JSON schema shape.
        assert "type" in tool["inputSchema"] or "properties" in tool["inputSchema"]
        # Deterministic: names are unique.
        assert tool["name"] not in seen_names
        seen_names.add(tool["name"])


def test_fixture_tools_list_response_envelope() -> None:
    response = tools_list_response()
    assert isinstance(response, dict)
    assert "tools" in response
    assert response["tools"] == list_tools()


def test_fixture_list_tools_is_deterministic() -> None:
    assert list_tools() == list_tools()


# ---------------------------------------------------------------------------
# Hardcoded-name guardrail


def test_no_hardcoded_tool_names_in_capability_layer() -> None:
    """Acceptance criterion 5: no `sg_nls_search`/`sg_deep_reason` in capability sources."""
    repo_root = Path(__file__).resolve().parents[2]
    targets = [
        repo_root / "src" / "codeprobe" / "mcp" / "capabilities.py",
        repo_root / "src" / "codeprobe" / "preambles" / "templates" / "mcp_base.md.j2",
    ]
    for path in targets:
        assert path.is_file(), f"Missing expected source file: {path}"
        content = path.read_text(encoding="utf-8")
        assert "sg_nls_search" not in content, f"Hardcoded tool name sg_nls_search in {path}"
        assert "sg_deep_reason" not in content, f"Hardcoded tool name sg_deep_reason in {path}"
