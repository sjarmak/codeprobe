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

from codeprobe.core.preamble import DefaultPreambleResolver  # noqa: E402


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


def test_resolver_loads_custom_preamble_path(tmp_path: Path):
    """Explicit .md preamble paths are loaded as trusted custom files."""
    task_dir = tmp_path / "task-001"
    task_dir.mkdir(parents=True)
    custom_path = tmp_path / "operator-preamble.md"
    custom_path.write_text("Use the operator checklist.", encoding="utf-8")

    resolver = DefaultPreambleResolver(task_dir=task_dir, project_dir=tmp_path)
    blocks = resolver.resolve([str(custom_path)])

    assert len(blocks) == 1
    assert blocks[0].name == str(custom_path)
    assert blocks[0].template == "Use the operator checklist."


def test_resolver_rejects_custom_preamble_path_outside_boundary(tmp_path: Path):
    """Relative custom paths may not escape the trusted project boundary."""
    project_dir = tmp_path / "repo"
    task_dir = project_dir / "tasks" / "task-001"
    task_dir.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")

    resolver = DefaultPreambleResolver(task_dir=task_dir, project_dir=project_dir)

    with pytest.raises(ValueError, match="outside trusted preamble roots"):
        resolver.resolve(["../outside.md"])


def test_resolver_rejects_custom_preamble_symlink_swap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A file swapped to an outside symlink after validation is never read."""
    project_dir = tmp_path / "repo"
    task_dir = project_dir / "tasks" / "task-001"
    task_dir.mkdir(parents=True)
    custom_path = project_dir / "operator-preamble.md"
    custom_path.write_text("trusted", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("outside secret", encoding="utf-8")
    original_is_file = Path.is_file

    def swap_after_stat(path: Path) -> bool:
        is_file = original_is_file(path)
        if path == custom_path:
            path.unlink()
            path.symlink_to(outside)
        return is_file

    monkeypatch.setattr(Path, "is_file", swap_after_stat)
    resolver = DefaultPreambleResolver(task_dir=task_dir, project_dir=project_dir)

    with pytest.raises(ValueError, match="changed during secure open"):
        resolver.resolve([str(custom_path)])


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

from codeprobe.core.preamble import compose_instruction, task_preamble_context  # noqa: E402


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


def test_compose_instruction_sourcegraph_symbol_reference_is_authoritative(
    tmp_path: Path,
):
    """symbol-reference-trace prompts should prefer authoritative references."""
    task_dir = tmp_path / "task-001"
    task_dir.mkdir(parents=True)
    resolver = DefaultPreambleResolver(task_dir=task_dir)

    prompt, _ = compose_instruction(
        instruction="Trace all references to MkdirAll.",
        repo_path=Path("/repo"),
        preamble_names=["sourcegraph"],
        resolver=resolver,
        task_id="task-001",
        extra_context=task_preamble_context(
            {
                "metadata": {
                    "category": "symbol-reference-trace",
                    "sg_repo": "github.com/acme/widgets",
                }
            }
        ),
    )

    assert "treat `mcp__sourcegraph__find_references` as authoritative" in prompt
    # jf28: under file-removal mode local search is unavailable; the
    # corresponding union forbidden is the MCP keyword-search union.
    assert "do not replace it with a keyword-search union" in prompt
    assert "maximum recall" not in prompt
    # Regression: symbol-reference-trace must explicitly forbid sg_deepsearch.
    # We saw an Opus 4.7 run burn 3 deepsearch calls (most of the cost) on
    # a task that find_references answered correctly on the first call.
    assert "DO NOT call `mcp__sourcegraph__deepsearch`" in prompt
    assert "do not escalate to `mcp__sourcegraph__deepsearch`" in prompt


def test_compose_instruction_sourcegraph_default_keeps_broad_recall_guidance(
    tmp_path: Path,
):
    """Non-authoritative families should keep the broad-recall guidance."""
    task_dir = tmp_path / "task-001"
    task_dir.mkdir(parents=True)
    resolver = DefaultPreambleResolver(task_dir=task_dir)

    prompt, _ = compose_instruction(
        instruction="Find the blast radius.",
        repo_path=Path("/repo"),
        preamble_names=["sourcegraph"],
        resolver=resolver,
        task_id="task-001",
        extra_context=task_preamble_context(
            {
                "metadata": {
                    "category": "change-scope-audit",
                    "sg_repo": "github.com/acme/widgets",
                }
            }
        ),
    )

    # jf28: under sg-only mode local search is no longer assumed available.
    # The broad recall directive is scoped to MCP-tool unions instead.
    assert "Union results when recall matters" in prompt
    assert "mcp__sourcegraph__keyword_search" in prompt
    assert "mcp__sourcegraph__nls_search" in prompt
    assert "treat `mcp__sourcegraph__find_references` as authoritative" not in prompt
    # Regression (codeprobe-riad): default branch must carry the
    # verify-before-denying rule. Sourcegraph false-negatives were
    # leading agents to write confident denials of existence
    # ("FlagAliases does not exist") even when broader queries would
    # find them. The default branch is the place to land this rule
    # because index-lag is generic, not category-specific.
    assert "Verify before denying existence" in prompt
    assert "Do not write a denial of existence" in prompt
    assert "Sourcegraph's index can lag the working tree" in prompt


def test_compose_instruction_sourcegraph_oracle_checks_uses_coverage_first(
    tmp_path: Path,
):
    """oracle_checks tasks should get coverage-first rubric guidance.

    Regression: 3oms eval saw oc_004 drop 1.000 → 0.643 because the default
    branch encouraged unioning grep results, which steered the agent away
    from the explicit `flag_aliases` rubric criterion. The oracle_checks
    branch must instead enforce criterion-by-criterion coverage.
    """
    task_dir = tmp_path / "oc-task"
    task_dir.mkdir(parents=True)
    resolver = DefaultPreambleResolver(task_dir=task_dir)

    prompt, _ = compose_instruction(
        instruction="Answer the rubric questions about feature flags.",
        repo_path=Path("/repo"),
        preamble_names=["sourcegraph"],
        resolver=resolver,
        task_id="oc_004",
        extra_context=task_preamble_context(
            {
                "metadata": {
                    "category": "oracle_checks",
                    "sg_repo": "github.com/acme/widgets",
                }
            }
        ),
    )

    # Coverage-first language must appear.
    assert "address every declared criterion" in prompt
    assert "Coverage-first synthesis" in prompt
    assert "list the explicit criteria" in prompt
    # Must NOT carry the broad-recall default that caused oc_004 to regress.
    assert "Union results when recall matters" not in prompt
    # Must NOT carry the symbol-reference-trace authoritative-only language.
    assert "treat `mcp__sourcegraph__find_references` as authoritative" not in prompt
    # Regression (codeprobe-riad): oracle_checks must carry an emphasised
    # verify-before-denying rule. oc_004 reproducibly failed because the
    # agent wrote "FlagAliases does not appear anywhere in the gascity
    # codebase" after Sourcegraph (with a stale index) returned no hits.
    # The rubric guarantees the symbol exists; the agent must broaden
    # MCP search before denying.
    assert "Verify before denying existence" in prompt
    assert "rubric guarantees the named symbol exists" in prompt
    assert "Never write a denial of existence" in prompt


def test_compose_instruction_sourcegraph_sdlc_uses_stop_signal(tmp_path: Path):
    """SDLC tasks should be told to stop searching once file list is known.

    Regression: 3oms eval saw SDLC family take +40% wall-clock under the
    default broad-recall preamble. The sdlc branch must steer the agent
    toward implementation effort rather than pre-discovery.
    """
    task_dir = tmp_path / "sdlc-task"
    task_dir.mkdir(parents=True)
    resolver = DefaultPreambleResolver(task_dir=task_dir)

    prompt, _ = compose_instruction(
        instruction="Modify src/foo/bar.py to add a new field.",
        repo_path=Path("/repo"),
        preamble_names=["sourcegraph"],
        resolver=resolver,
        task_id="0d4ec3ad",
        extra_context=task_preamble_context(
            {
                "metadata": {
                    "category": "sdlc",
                    "sg_repo": "github.com/acme/widgets",
                }
            }
        ),
    )

    # Stop-signal language must appear.
    assert "Stop searching when you have the file list" in prompt
    assert "Implementation effort is the bottleneck" in prompt
    assert "NOT to pre-discover file lists" in prompt
    # Must NOT carry the broad-recall default.
    assert "Union results when recall matters" not in prompt
    # Must NOT carry the oracle_checks coverage-first language.
    assert "Coverage-first synthesis" not in prompt


def test_compose_instruction_sourcegraph_strict_uses_only_mcp_tool_names(
    tmp_path: Path,
):
    """Strict Sourcegraph prompts must not direct agents to blocked built-ins."""
    task_dir = tmp_path / "sdlc-task"
    task_dir.mkdir(parents=True)
    resolver = DefaultPreambleResolver(task_dir=task_dir)

    prompt, _ = compose_instruction(
        instruction="Add a new field.",
        repo_path=Path("/work/grpc-go"),
        preamble_names=["sourcegraph"],
        resolver=resolver,
        task_id="sdlc-001",
        extra_context=task_preamble_context(
            {
                "metadata": {
                    "category": "sdlc",
                    "sg_repo": "github.com/acme/grpc-go",
                }
            }
        ),
    )

    assert "Strict MCP mode is active" in prompt
    assert "mcp__sourcegraph__read_file" in prompt
    assert "mcp__sourcegraph__keyword_search" in prompt
    for blocked in ("`Read`", "`Grep`", "`Glob`", "`Bash`"):
        assert blocked not in prompt
    for legacy in ("sg_read_file", "sg_keyword_search", "sg_find_references"):
        assert legacy not in prompt
    # repo_scope is rendered with the actual sg_repo value, not left
    # as a literal template token.
    assert "github.com/acme/grpc-go" in prompt
    assert "{{repo_scope}}" not in prompt
    assert "{{workflow_tail}}" not in prompt
    # The EFFICIENCY rule still reaches the SDLC branch.
    assert "Efficiency" in prompt


def test_compose_instruction_sourcegraph_pragmatic_keeps_local_read_explicit(
    tmp_path: Path,
):
    """Non-strict Sourcegraph prompts name local tools only when policy allows them."""
    task_dir = tmp_path / "sdlc-task"
    task_dir.mkdir(parents=True)
    resolver = DefaultPreambleResolver(task_dir=task_dir)

    prompt, _ = compose_instruction(
        instruction="Add a new field.",
        repo_path=Path("/work/grpc-go"),
        preamble_names=["sourcegraph"],
        resolver=resolver,
        task_id="sdlc-001",
        extra_context=task_preamble_context(
            {
                "metadata": {
                    "category": "sdlc",
                    "sg_repo": "github.com/acme/grpc-go",
                }
            },
            mcp_mode="pragmatic",
        ),
    )

    assert "Pragmatic MCP mode is active" in prompt
    assert "`Read` is available" in prompt
    assert "mcp__sourcegraph__read_file" in prompt
    for blocked in ("`Grep`", "`Glob`", "`Bash`"):
        assert blocked not in prompt


def test_compose_instruction_sourcegraph_uses_runtime_mcp_server_key(
    tmp_path: Path,
):
    """Sourcegraph tool names must match the configured MCP server key."""
    task_dir = tmp_path / "sdlc-task"
    task_dir.mkdir(parents=True)
    resolver = DefaultPreambleResolver(task_dir=task_dir)

    prompt, _ = compose_instruction(
        instruction="Add a new field.",
        repo_path=Path("/work/grpc-go"),
        preamble_names=["sourcegraph"],
        resolver=resolver,
        task_id="sdlc-001",
        extra_context=task_preamble_context(
            {
                "metadata": {
                    "category": "sdlc",
                    "sg_repo": "github.com/acme/grpc-go",
                }
            },
            mcp_mode="strict",
            mcp_config={"mcpServers": {"sg": {"type": "http"}}},
        ),
    )

    assert "mcp__sg__keyword_search" in prompt
    assert "mcp__sg__find_references" in prompt
    assert "mcp__sg__read_file" in prompt
    assert "mcp__sourcegraph__" not in prompt


def test_compose_instruction_sourcegraph_efficiency_rule_in_all_branches(
    tmp_path: Path,
):
    """All four category branches must carry the 'don't over-read' efficiency rule.

    Regression (codeprobe-evjr): Section 6.2 audit recommended placing the
    EFFICIENCY guard in every branch of `task_preamble_context` so MCP-only
    reading patterns don't survive when categories switch. Verifies the
    SDLC, default, oracle_checks, and symbol-reference-trace branches all
    include the EFFICIENCY rule.
    """
    task_dir = tmp_path / "task-001"
    task_dir.mkdir(parents=True)
    resolver = DefaultPreambleResolver(task_dir=task_dir)

    categories = [
        "sdlc",
        "oracle_checks",
        "symbol-reference-trace",
        "change-scope-audit",  # exercises the default branch
    ]
    for category in categories:
        prompt, _ = compose_instruction(
            instruction="Do the task.",
            repo_path=Path("/repo"),
            preamble_names=["sourcegraph"],
            resolver=resolver,
            task_id="task-001",
            extra_context=task_preamble_context(
                {
                    "metadata": {
                        "category": category,
                        "sg_repo": "github.com/acme/widgets",
                    }
                }
            ),
        )
        assert "Efficiency" in prompt, (
            f"Category {category!r} missing EFFICIENCY rule in synthesis step"
        )


# -- task_preamble_context guard tests ---------------------------------------


def test_task_preamble_context_raises_when_sg_repo_empty_with_sourcegraph():
    """Empty ``metadata.sg_repo`` plus a ``sourcegraph`` preamble is fatal.

    Regression (codeprobe-evjr.3): gascity SDLC tasks were shipping with
    ``metadata.sg_repo = ""``, which caused the Sourcegraph preamble's
    ``repo:^{{sg_repo}}$`` filter to render as ``repo:^$ <query>`` — a
    malformed scope that fell back to global search and inflated cost
    without scoping. ``task_preamble_context`` must convert this silent
    degradation into a fail-loud error at prompt-composition time.
    """
    with pytest.raises(ValueError, match="sg_repo is empty"):
        task_preamble_context(
            {"metadata": {"category": "sdlc", "sg_repo": ""}},
            preamble_names=["sourcegraph"],
            task_id="ba1f3675",
        )


def test_task_preamble_context_raises_when_sg_repo_missing_with_sourcegraph():
    """Missing ``metadata.sg_repo`` key triggers the same guard."""
    with pytest.raises(ValueError, match="ba1f3675"):
        task_preamble_context(
            {"metadata": {"category": "sdlc"}},
            preamble_names=["sourcegraph"],
            task_id="ba1f3675",
        )


def test_task_preamble_context_no_guard_without_sourcegraph_preamble():
    """Empty ``sg_repo`` is tolerated when sourcegraph isn't requested."""
    ctx = task_preamble_context(
        {"metadata": {"category": "sdlc", "sg_repo": ""}},
        preamble_names=["github"],
        task_id="ba1f3675",
    )
    assert "sg_repo" not in ctx


def test_task_preamble_context_no_guard_when_preamble_names_omitted():
    """Backwards-compatible callers (no ``preamble_names``) don't trigger."""
    ctx = task_preamble_context(
        {"metadata": {"category": "sdlc", "sg_repo": ""}},
    )
    assert "sg_repo" not in ctx


def test_task_preamble_context_passes_when_sg_repo_populated():
    """Populated ``sg_repo`` flows through and propagates to the context."""
    ctx = task_preamble_context(
        {
            "metadata": {
                "category": "sdlc",
                "sg_repo": "github.com/gastownhall/gascity",
            }
        },
        preamble_names=["sourcegraph"],
        task_id="ba1f3675",
    )
    assert ctx["sg_repo"] == "github.com/gastownhall/gascity"


# -- Built-in preamble tests --------------------------------------------------

from codeprobe.preambles import get_builtin, list_builtins  # noqa: E402


def test_builtin_sourcegraph_preamble_exists():
    """The sourcegraph preamble ships as a built-in.

    jf28: The v2 template references ``{{repo_scope}}`` and
    ``{{workflow_tail}}`` rather than substituting ``{{sg_repo}}``
    directly. The repo identifier is composed into ``repo_scope`` by
    ``task_preamble_context`` and substituted at compose time.
    """
    block = get_builtin("sourcegraph")
    assert block.name == "sourcegraph"
    assert "{{sourcegraph_tool_prefix}}__keyword_search" in block.template
    assert "mcp__sourcegraph__" not in block.template
    assert "sg_keyword_search" not in block.template
    assert "{{repo_scope}}" in block.template
    assert "{{workflow_tail}}" in block.template
    assert "{{source_access_policy}}" in block.template


def test_builtin_preamble_renders_variables():
    """Built-in preamble template variables resolve correctly.

    jf28: The renderer fills ``{{repo_scope}}`` and ``{{workflow_tail}}``
    via the slots produced by ``task_preamble_context``. We exercise
    ``compose_instruction`` end-to-end below; this test covers the raw
    block.render() contract — both slots are substituted, no literal
    template tokens leak through.
    """
    block = get_builtin("sourcegraph")
    rendered = block.render(
        {
            "repo_path": "/my/repo",
            "repo_name": "my-repo",
            "task_id": "task-42",
            "repo_scope": "**Indexed repository:** `github.com/sg-evals/my-repo`.",
            "workflow_tail": "3. **Trace references** — example tail step.",
            "source_access_policy": "Strict MCP mode is active.",
        }
    )
    assert "github.com/sg-evals/my-repo" in rendered
    assert "Trace references" in rendered
    assert "{{repo_scope}}" not in rendered
    assert "{{workflow_tail}}" not in rendered
    assert "{{source_access_policy}}" not in rendered


def test_builtin_github_preamble_exists():
    """The github preamble ships as a built-in.

    r12-capability-preambles hardened this preamble to be capability-first
    rather than naming specific MCP tool names (which vary by server). The
    preamble now references capability IDs that must map to the registry.
    """
    block = get_builtin("github")
    assert block.name == "github"
    # Capability-first content (see src/codeprobe/mcp/capabilities.py).
    assert "KEYWORD_SEARCH" in block.template
    assert "FILE_READ" in block.template


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
