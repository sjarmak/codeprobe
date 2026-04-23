"""Tests for the generic 'custom' preamble renderer.

The custom preamble must never hardcode tool names — every tool name and
description comes from the caller-supplied tool table. These tests pin
that contract by substituting a fixture tool name and asserting it
appears verbatim in the rendered Markdown.
"""

from __future__ import annotations

import pytest

from codeprobe.mcp.capabilities import CAPABILITIES
from codeprobe.preambles.generator import render_preamble


@pytest.fixture
def fixture_tool_table() -> list[dict[str, str]]:
    """A made-up tool table — tool names chosen to be obviously synthetic
    so we can assert they round-trip into the rendered output."""
    return [
        {
            "name": "acme_super_search",
            "description": "Exact keyword lookup against the acme index.",
            "capability_id": "KEYWORD_SEARCH",
        },
        {
            "name": "acme_refs",
            "description": "Find every caller of a given symbol.",
            "capability_id": "SYMBOL_REFERENCES",
        },
        {
            "name": "acme_read",
            "description": "Fetch a file from the indexed repo.",
            "capability_id": "FILE_READ",
        },
    ]


def test_render_preamble_includes_fixture_tool_names(
    fixture_tool_table: list[dict[str, str]],
) -> None:
    rendered = render_preamble(
        capability_set=["KEYWORD_SEARCH", "SYMBOL_REFERENCES", "FILE_READ"],
        tool_table=fixture_tool_table,
    )
    for tool in fixture_tool_table:
        assert tool["name"] in rendered, (
            f"expected fixture tool name {tool['name']!r} in rendered preamble; "
            f"output:\n{rendered}"
        )


def test_render_preamble_includes_capability_names_and_ids(
    fixture_tool_table: list[dict[str, str]],
) -> None:
    capability_set = ["KEYWORD_SEARCH", "SYMBOL_REFERENCES", "FILE_READ"]
    rendered = render_preamble(capability_set, fixture_tool_table)
    for cap_id in capability_set:
        # The capability ID and its human-readable name should both render.
        assert cap_id in rendered
        assert CAPABILITIES[cap_id].name in rendered


def test_render_preamble_does_not_hardcode_vendor_tool_names(
    fixture_tool_table: list[dict[str, str]],
) -> None:
    """No Sourcegraph / GitHub tool names should leak into the custom template."""
    rendered = render_preamble(
        capability_set=sorted(CAPABILITIES.keys()),
        tool_table=fixture_tool_table,
    )
    forbidden = [
        "sg_keyword_search",
        "sg_nls_search",
        "sg_find_references",
        "sg_read_file",
        "search_code",
        "get_file_contents",
    ]
    for name in forbidden:
        assert name not in rendered, (
            f"custom template must not hardcode vendor tool name {name!r}"
        )


def test_render_preamble_subset_of_capabilities(
    fixture_tool_table: list[dict[str, str]],
) -> None:
    """When only one capability is requested, only that capability's name
    appears in the Capabilities bullet list."""
    rendered = render_preamble(
        capability_set=["KEYWORD_SEARCH"],
        tool_table=fixture_tool_table[:1],
    )
    assert "keyword_search" in rendered
    # Other capability names should not appear.
    assert "go_to_definition" not in rendered


def test_render_preamble_rejects_unknown_capability(
    fixture_tool_table: list[dict[str, str]],
) -> None:
    with pytest.raises(KeyError, match="NOT_A_REAL_CAP"):
        render_preamble(
            capability_set=["NOT_A_REAL_CAP"],
            tool_table=fixture_tool_table,
        )


def test_render_preamble_rejects_empty_inputs() -> None:
    with pytest.raises(ValueError):
        render_preamble(
            capability_set=[],
            tool_table=[
                {
                    "name": "x",
                    "description": "y",
                    "capability_id": "KEYWORD_SEARCH",
                }
            ],
        )
    with pytest.raises(ValueError):
        render_preamble(capability_set=["KEYWORD_SEARCH"], tool_table=[])


def test_render_preamble_rejects_malformed_tool_table() -> None:
    with pytest.raises(ValueError, match="missing required keys"):
        render_preamble(
            capability_set=["KEYWORD_SEARCH"],
            tool_table=[{"name": "x"}],
        )
    with pytest.raises(TypeError):
        render_preamble(
            capability_set=["KEYWORD_SEARCH"],
            tool_table=["not-a-mapping"],  # type: ignore[list-item]
        )


def test_render_preamble_returns_markdown_not_empty(
    fixture_tool_table: list[dict[str, str]],
) -> None:
    rendered = render_preamble(
        capability_set=sorted(CAPABILITIES.keys()),
        tool_table=fixture_tool_table,
    )
    assert rendered.strip()
    assert rendered.startswith("# ")  # Markdown heading
