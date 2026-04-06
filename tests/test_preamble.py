"""Tests for preamble model, resolver, compose_instruction, and built-ins."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codeprobe.models.preamble import PreambleBlock

# -- PreambleBlock model tests ------------------------------------------------


def test_preamble_block_is_frozen():
    block = PreambleBlock(name="test", template="hello {{repo_path}}")
    with pytest.raises(AttributeError):
        block.name = "changed"  # type: ignore[misc]


def test_preamble_block_defaults():
    block = PreambleBlock(name="base", template="content")
    assert block.description == ""


def test_preamble_block_render_variables():
    block = PreambleBlock(
        name="ctx",
        template="Repo: {{repo_path}}, Task: {{task_id}}",
    )
    context = {"repo_path": "/home/user/repo", "task_id": "task-001"}
    result = block.render(context)
    assert result == "Repo: /home/user/repo, Task: task-001"


def test_preamble_block_render_no_variables():
    block = PreambleBlock(name="static", template="No variables here.")
    result = block.render({})
    assert result == "No variables here."


def test_preamble_block_render_unknown_variable_left_intact():
    """Unknown {{variables}} are left as-is (no crash, no silent removal)."""
    block = PreambleBlock(name="partial", template="{{known}} and {{unknown}}")
    result = block.render({"known": "resolved"})
    assert result == "resolved and {{unknown}}"


# -- DefaultPreambleResolver tests -------------------------------------------

from codeprobe.core.preamble import DefaultPreambleResolver


def test_resolver_finds_task_local_preamble(tmp_path: Path):
    """Resolver finds preamble in task-local preambles/ directory."""
    task_dir = tmp_path / "task-001"
    preambles = task_dir / "preambles"
    preambles.mkdir(parents=True)
    (preambles / "hint.md").write_text("Use TDD approach.")

    resolver = DefaultPreambleResolver(task_dir=task_dir)
    blocks = resolver.resolve(["hint"])
    assert len(blocks) == 1
    assert blocks[0].name == "hint"
    assert blocks[0].template == "Use TDD approach."


def test_resolver_project_dir_fallback(tmp_path: Path):
    """Resolver falls back to project .codeprobe/preambles/ directory."""
    task_dir = tmp_path / "task-001"
    task_dir.mkdir(parents=True)
    project_preambles = tmp_path / ".codeprobe" / "preambles"
    project_preambles.mkdir(parents=True)
    (project_preambles / "style.md").write_text("Be concise.")

    resolver = DefaultPreambleResolver(
        task_dir=task_dir,
        project_dir=tmp_path,
    )
    blocks = resolver.resolve(["style"])
    assert len(blocks) == 1
    assert blocks[0].template == "Be concise."


def test_resolver_user_dir_fallback(tmp_path: Path):
    """Resolver falls back to user ~/.codeprobe/preambles/ directory."""
    task_dir = tmp_path / "task-001"
    task_dir.mkdir(parents=True)
    user_preambles = tmp_path / "user_home" / ".codeprobe" / "preambles"
    user_preambles.mkdir(parents=True)
    (user_preambles / "global.md").write_text("Global preamble.")

    resolver = DefaultPreambleResolver(
        task_dir=task_dir,
        user_dir=tmp_path / "user_home",
    )
    blocks = resolver.resolve(["global"])
    assert len(blocks) == 1
    assert blocks[0].template == "Global preamble."


def test_resolver_task_local_takes_precedence(tmp_path: Path):
    """Task-local preamble overrides project and user preambles."""
    task_dir = tmp_path / "task-001"
    task_preambles = task_dir / "preambles"
    task_preambles.mkdir(parents=True)
    (task_preambles / "hint.md").write_text("task-local version")

    project_preambles = tmp_path / ".codeprobe" / "preambles"
    project_preambles.mkdir(parents=True)
    (project_preambles / "hint.md").write_text("project version")

    resolver = DefaultPreambleResolver(
        task_dir=task_dir,
        project_dir=tmp_path,
    )
    blocks = resolver.resolve(["hint"])
    assert len(blocks) == 1
    assert blocks[0].template == "task-local version"


def test_resolver_multiple_names(tmp_path: Path):
    """Resolver resolves multiple preamble names in order."""
    task_dir = tmp_path / "task-001"
    preambles = task_dir / "preambles"
    preambles.mkdir(parents=True)
    (preambles / "a.md").write_text("First")
    (preambles / "b.md").write_text("Second")

    resolver = DefaultPreambleResolver(task_dir=task_dir)
    blocks = resolver.resolve(["a", "b"])
    assert len(blocks) == 2
    assert blocks[0].template == "First"
    assert blocks[1].template == "Second"


def test_resolver_missing_preamble_raises(tmp_path: Path):
    """Resolver raises FileNotFoundError for unknown preamble names."""
    task_dir = tmp_path / "task-001"
    task_dir.mkdir(parents=True)

    resolver = DefaultPreambleResolver(task_dir=task_dir)
    with pytest.raises(FileNotFoundError, match="nonexistent"):
        resolver.resolve(["nonexistent"])


def test_resolver_empty_names_returns_empty(tmp_path: Path):
    """Resolver returns empty list when no names are requested."""
    task_dir = tmp_path / "task-001"
    task_dir.mkdir(parents=True)

    resolver = DefaultPreambleResolver(task_dir=task_dir)
    blocks = resolver.resolve([])
    assert blocks == []


def test_resolver_rejects_path_traversal(tmp_path: Path):
    """Preamble names with path separators or '..' are rejected."""
    task_dir = tmp_path / "task-001"
    task_dir.mkdir(parents=True)

    resolver = DefaultPreambleResolver(task_dir=task_dir)
    with pytest.raises(ValueError, match="illegal path characters"):
        resolver.resolve(["../secret"])

    with pytest.raises(ValueError, match="illegal path characters"):
        resolver.resolve(["sub/dir"])


# -- compose_instruction tests -----------------------------------------------

from codeprobe.core.preamble import compose_instruction


def test_compose_instruction_empty_preambles(tmp_path: Path):
    """With empty preamble list, returns base prompt and no resolved blocks."""
    task_dir = tmp_path / "task-001"
    task_dir.mkdir(parents=True)
    resolver = DefaultPreambleResolver(task_dir=task_dir)

    prompt, resolved = compose_instruction(
        instruction="Fix the bug.",
        repo_path=Path("/repo"),
        preamble_names=[],
        resolver=resolver,
    )
    assert "Fix the bug." in prompt
    assert "/repo" in prompt
    assert resolved == []


def test_compose_instruction_with_preambles(tmp_path: Path):
    """Preambles are appended after base instruction wrapper."""
    task_dir = tmp_path / "task-001"
    preambles_dir = task_dir / "preambles"
    preambles_dir.mkdir(parents=True)
    (preambles_dir / "tdd.md").write_text("Write tests first.")

    resolver = DefaultPreambleResolver(task_dir=task_dir)

    prompt, resolved = compose_instruction(
        instruction="Fix the bug.",
        repo_path=Path("/repo"),
        preamble_names=["tdd"],
        resolver=resolver,
    )
    assert "Fix the bug." in prompt
    assert "Write tests first." in prompt
    assert len(resolved) == 1


def test_compose_instruction_renders_variables(tmp_path: Path):
    """Template variables are rendered in preamble blocks."""
    task_dir = tmp_path / "task-001"
    preambles_dir = task_dir / "preambles"
    preambles_dir.mkdir(parents=True)
    (preambles_dir / "ctx.md").write_text("Working on {{repo_path}}, task {{task_id}}")

    resolver = DefaultPreambleResolver(task_dir=task_dir)

    prompt, resolved = compose_instruction(
        instruction="Do the thing.",
        repo_path=Path("/my/repo"),
        preamble_names=["ctx"],
        resolver=resolver,
        task_id="task-001",
    )
    assert "/my/repo" in prompt
    assert "task-001" in prompt


def test_compose_instruction_returns_resolved_content(tmp_path: Path):
    """compose_instruction returns a tuple of (prompt, resolved_preambles) for reproducibility."""
    task_dir = tmp_path / "task-001"
    preambles_dir = task_dir / "preambles"
    preambles_dir.mkdir(parents=True)
    (preambles_dir / "hint.md").write_text("Be careful.")

    resolver = DefaultPreambleResolver(task_dir=task_dir)

    prompt, resolved = compose_instruction(
        instruction="Fix it.",
        repo_path=Path("/repo"),
        preamble_names=["hint"],
        resolver=resolver,
    )
    assert isinstance(prompt, str)
    assert isinstance(resolved, list)
    assert len(resolved) == 1
    assert resolved[0]["name"] == "hint"
    assert resolved[0]["content"] == "Be careful."


# -- Built-in preamble tests --------------------------------------------------

from codeprobe.preambles import get_builtin, list_builtins


def test_builtin_sourcegraph_preamble_exists():
    """The sourcegraph preamble ships as a built-in."""
    block = get_builtin("sourcegraph")
    assert block.name == "sourcegraph"
    assert "keyword_search" in block.template
    assert "{{repo_name}}" in block.template


def test_builtin_preamble_renders_variables():
    """Built-in preamble template variables resolve correctly."""
    block = get_builtin("sourcegraph")
    rendered = block.render(
        {"repo_path": "/my/repo", "repo_name": "my-repo", "task_id": "task-42"}
    )
    assert "my-repo" in rendered
    assert "{{repo_name}}" not in rendered


def test_builtin_nonexistent_raises():
    """get_builtin raises KeyError for unknown names."""
    with pytest.raises(KeyError, match="no-such-preamble"):
        get_builtin("no-such-preamble")


def test_list_builtins_includes_sourcegraph():
    """list_builtins returns at least the sourcegraph preamble."""
    names = list_builtins()
    assert "sourcegraph" in names


# -- Resolver built-in fallback tests -----------------------------------------


def test_resolver_falls_back_to_builtin(tmp_path: Path):
    """Resolver finds built-in preamble when not in any search directory."""
    task_dir = tmp_path / "task-001"
    task_dir.mkdir(parents=True)

    resolver = DefaultPreambleResolver(task_dir=task_dir)
    blocks = resolver.resolve(["sourcegraph"])
    assert len(blocks) == 1
    assert blocks[0].name == "sourcegraph"
    assert "keyword_search" in blocks[0].template


def test_resolver_local_overrides_builtin(tmp_path: Path):
    """Task-local preamble takes precedence over built-in."""
    task_dir = tmp_path / "task-001"
    preambles = task_dir / "preambles"
    preambles.mkdir(parents=True)
    (preambles / "sourcegraph.md").write_text("Custom sourcegraph preamble.")

    resolver = DefaultPreambleResolver(task_dir=task_dir)
    blocks = resolver.resolve(["sourcegraph"])
    assert len(blocks) == 1
    assert blocks[0].template == "Custom sourcegraph preamble."


# -- Template file tests ------------------------------------------------------


def test_template_files_are_valid_yaml():
    """All shipped evalrc template YAML files parse without error."""
    templates_dir = (
        Path(__file__).resolve().parent.parent / "src" / "codeprobe" / "templates"
    )
    yaml_files = list(templates_dir.glob("*.yaml"))
    assert (
        len(yaml_files) >= 3
    ), f"Expected at least 3 templates, found {len(yaml_files)}"

    for yaml_file in yaml_files:
        content = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
        assert isinstance(content, dict), f"{yaml_file.name} did not parse as dict"
        assert "name" in content, f"{yaml_file.name} missing 'name' field"
        assert "dimensions" in content, f"{yaml_file.name} missing 'dimensions' field"


def test_mcp_template_references_sourcegraph_preamble():
    """MCP comparison template uses the built-in sourcegraph preamble."""
    templates_dir = (
        Path(__file__).resolve().parent.parent / "src" / "codeprobe" / "templates"
    )
    content = yaml.safe_load((templates_dir / "evalrc-mcp-comparison.yaml").read_text())
    prompts = content["dimensions"]["prompts"]
    assert "with-mcp-hints" in prompts
    hints = prompts["with-mcp-hints"]
    preambles = hints.get("preambles", hints) if isinstance(hints, dict) else hints
    assert "sourcegraph" in preambles
