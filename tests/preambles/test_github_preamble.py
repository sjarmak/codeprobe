"""Tests for the built-in 'github' preamble.

AC #1 of r12-capability-preambles: github.md is ≥50 lines, capability-first,
and does NOT hardcode Sourcegraph-specific tool names.
AC #4: `--preamble github` resolves end-to-end into the composed instruction.
"""

from __future__ import annotations

from pathlib import Path

from codeprobe.core.preamble import DefaultPreambleResolver, compose_instruction
from codeprobe.preambles import get_builtin


def test_github_preamble_is_at_least_fifty_lines() -> None:
    block = get_builtin("github")
    line_count = block.template.count("\n") + 1
    assert line_count >= 50, (
        f"github.md must be ≥50 lines (AC #1); got {line_count}"
    )


def test_github_preamble_has_no_sourcegraph_tool_names() -> None:
    block = get_builtin("github")
    forbidden = [
        "sg_keyword_search",
        "sg_nls_search",
        "sg_find_references",
        "sg_go_to_definition",
        "sg_read_file",
        "sg_list_files",
        "sg_commit_search",
        "sg_diff_search",
        "sg_deepsearch",
    ]
    body = block.template
    for name in forbidden:
        assert name not in body, (
            f"github.md must not hardcode Sourcegraph tool name {name!r}"
        )


def test_github_preamble_references_capability_ids() -> None:
    """The preamble should talk about capabilities by stable ID so it
    remains valid when the backing MCP server changes."""
    block = get_builtin("github")
    body = block.template
    for cap_id in ("KEYWORD_SEARCH", "SYMBOL_REFERENCES", "FILE_READ", "GO_TO_DEFINITION"):
        assert cap_id in body, (
            f"github.md should reference capability {cap_id!r} by ID"
        )


def test_github_preamble_resolves_through_default_resolver(tmp_path: Path) -> None:
    """AC #4: --preamble github resolves end-to-end via the default resolver."""
    resolver = DefaultPreambleResolver(
        task_dir=tmp_path,
        project_dir=tmp_path / "project",
        user_dir=tmp_path / "user",
    )
    prompt, resolved = compose_instruction(
        instruction="Do the task.",
        repo_path=tmp_path / "repo",
        preamble_names=["github"],
        resolver=resolver,
        task_id="task-r12-1",
    )
    assert any(r["name"] == "github" for r in resolved)
    github_content = next(r["content"] for r in resolved if r["name"] == "github")
    # Capability IDs must make it into the composed prompt.
    assert "KEYWORD_SEARCH" in github_content
    assert "KEYWORD_SEARCH" in prompt
    # Basic shape: prompt starts with the base wrapper, then the preamble.
    assert "Do the task." in prompt


def test_github_preamble_template_vars_substituted(tmp_path: Path) -> None:
    """`{{sg_repo}}` and `{{repo_name}}` placeholders should substitute."""
    resolver = DefaultPreambleResolver(task_dir=tmp_path)
    repo_path = tmp_path / "my-repo"
    prompt, _ = compose_instruction(
        instruction="Task body.",
        repo_path=repo_path,
        preamble_names=["github"],
        resolver=resolver,
        task_id="t1",
        extra_context={"sg_repo": "github.com/acme/widgets"},
    )
    assert "github.com/acme/widgets" in prompt
    assert "my-repo" in prompt
