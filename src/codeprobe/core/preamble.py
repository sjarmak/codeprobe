"""Preamble resolution and instruction composition."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Protocol, runtime_checkable

from codeprobe.models.preamble import PreambleBlock
from codeprobe.preambles import get_builtin

__all__ = [
    "PreambleResolver",
    "DefaultPreambleResolver",
    "normalize_preamble_references",
    "base_prompt",
    "compose_instruction",
    "task_preamble_context",
]

_SYMBOL_REFERENCE_TRACE_CATEGORY = "symbol-reference-trace"
_ORACLE_CHECKS_CATEGORY = "oracle_checks"
_SDLC_CATEGORY = "sdlc"
_SOURCEGRAPH_KEYWORD_SEARCH = "mcp__sourcegraph__keyword_search"
_SOURCEGRAPH_NLS_SEARCH = "mcp__sourcegraph__nls_search"
_SOURCEGRAPH_FIND_REFERENCES = "mcp__sourcegraph__find_references"
_SOURCEGRAPH_GO_TO_DEFINITION = "mcp__sourcegraph__go_to_definition"
_SOURCEGRAPH_READ_FILE = "mcp__sourcegraph__read_file"
_SOURCEGRAPH_LIST_FILES = "mcp__sourcegraph__list_files"
_SOURCEGRAPH_LIST_REPOS = "mcp__sourcegraph__list_repos"
_SOURCEGRAPH_COMMIT_SEARCH = "mcp__sourcegraph__commit_search"
_SOURCEGRAPH_DIFF_SEARCH = "mcp__sourcegraph__diff_search"
_SOURCEGRAPH_COMPARE_REVISIONS = "mcp__sourcegraph__compare_revisions"
_SOURCEGRAPH_DEEPSEARCH = "mcp__sourcegraph__deepsearch"
_SOURCEGRAPH_DEEPSEARCH_READ = "mcp__sourcegraph__deepsearch_read"
_MCP_MODES = frozenset({"strict", "pragmatic", "loose"})

# Preambles whose templates render ``repo:^{{sg_repo}}$`` and therefore
# silently degrade to a malformed ``repo:^$`` filter when ``sg_repo`` is
# missing. ``github`` explicitly falls back to ``repo_name`` and is not
# included here.
_SG_REPO_REQUIRED_PREAMBLES = frozenset({"sourcegraph"})


@runtime_checkable
class PreambleResolver(Protocol):
    """Protocol for resolving preamble names to PreambleBlock instances."""

    def resolve(self, names: list[str]) -> list[PreambleBlock]:
        """Resolve preamble names to loaded blocks.

        Raises FileNotFoundError if a name cannot be found in any search path.
        """
        ...


class DefaultPreambleResolver:
    """Resolve preamble names from a layered directory chain.

    Search order (first match wins):
      1. task-local ``preambles/`` directory
      2. project ``.codeprobe/preambles/`` directory
      3. user ``~/.codeprobe/preambles/`` directory
    """

    def __init__(
        self,
        task_dir: Path,
        project_dir: Path | None = None,
        user_dir: Path | None = None,
        custom_base_dir: Path | None = None,
    ) -> None:
        self._search_dirs: list[Path] = []
        self._custom_base_dir = (
            custom_base_dir if custom_base_dir is not None else project_dir or task_dir
        ).resolve()
        custom_roots = [task_dir]

        # Task-local (highest priority)
        self._search_dirs.append(task_dir / "preambles")

        # Project-level
        if project_dir is not None:
            self._search_dirs.append(project_dir / ".codeprobe" / "preambles")
            custom_roots.append(project_dir)

        # User-level
        if user_dir is not None:
            self._search_dirs.append(user_dir / ".codeprobe" / "preambles")
            custom_roots.append(user_dir)

        self._custom_roots = tuple(root.resolve() for root in custom_roots)

    def resolve(self, names: list[str]) -> list[PreambleBlock]:
        """Resolve each name by searching directories in priority order."""
        return [self._resolve_one(name) for name in names]

    def normalize_reference(self, name: str) -> str:
        """Return the canonical value to persist for a preamble reference."""
        if self._is_custom_file_reference(name):
            return str(self._resolve_custom_file_path(name))
        return name

    def _resolve_one(self, name: str) -> PreambleBlock:
        if self._is_custom_file_reference(name):
            path = self._resolve_custom_file_path(name)
            if not path.is_file():
                raise FileNotFoundError(f"Custom preamble path not found: {path}")
            return PreambleBlock(
                name=str(path),
                template=path.read_text(encoding="utf-8").strip(),
            )

        if "/" in name or "\\" in name or ".." in name:
            raise ValueError(
                f"Preamble name contains illegal path characters: {name!r}"
            )
        for search_dir in self._search_dirs:
            path = (search_dir / f"{name}.md").resolve()
            if not str(path).startswith(str(search_dir.resolve())):
                raise ValueError(f"Preamble name escapes search directory: {name!r}")
            if path.is_file():
                return PreambleBlock(
                    name=name,
                    template=path.read_text(encoding="utf-8").strip(),
                )
        # Fall back to built-in preambles shipped with codeprobe
        try:
            return get_builtin(name)
        except KeyError:
            pass

        raise FileNotFoundError(
            f"Preamble {name!r} not found in search paths: "
            f"{[str(d) for d in self._search_dirs]}"
        )

    def _is_custom_file_reference(self, name: str) -> bool:
        return Path(name).suffix.lower() == ".md"

    def _resolve_custom_file_path(self, name: str) -> Path:
        if "\\" in name:
            raise ValueError(
                f"Custom preamble path contains unsupported separator: {name!r}"
            )
        raw_path = Path(name).expanduser()
        candidate = raw_path if raw_path.is_absolute() else self._custom_base_dir / raw_path
        resolved = candidate.resolve()
        if not any(_is_relative_to(resolved, root) for root in self._custom_roots):
            raise ValueError(
                f"Custom preamble path {name!r} resolves outside trusted "
                f"preamble roots: {[str(root) for root in self._custom_roots]}"
            )
        return resolved


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def normalize_preamble_references(
    names: Iterable[str],
    *,
    project_dir: Path,
    task_dir: Path | None = None,
    user_dir: Path | None = None,
    custom_base_dir: Path | None = None,
) -> tuple[str, ...]:
    """Normalize explicit custom preamble file paths for config persistence."""
    resolver = DefaultPreambleResolver(
        task_dir=task_dir or project_dir,
        project_dir=project_dir,
        user_dir=user_dir,
        custom_base_dir=custom_base_dir,
    )
    return tuple(resolver.normalize_reference(name) for name in names)


def base_prompt(
    instruction: str, repo_path: Path, *, worktree_path: Path | None = None
) -> str:
    """Build the base prompt wrapper shared across prompt-building paths.

    When *worktree_path* is provided (parallel isolation mode), the prompt
    references the worktree instead of the original repo path.

    This is the public API entry point; the legacy private alias
    ``_base_prompt`` is kept for backward compatibility with callers that
    imported it before the promotion to public surface (batch-a review).
    """
    effective_path = worktree_path if worktree_path is not None else repo_path
    # Rewrite TASK_REPO_ROOT in the instruction so agents write to the
    # worktree, not the original repo (avoids cross-task collisions and
    # ensures answer.txt lands where the executor expects it).
    if worktree_path is not None:
        instruction = instruction.replace(
            f"TASK_REPO_ROOT={repo_path}",
            f"TASK_REPO_ROOT={worktree_path}",
        )
    return (
        f"You are working on the repository at {effective_path}. "
        "Follow the instruction below.\n\n"
        f"{instruction}"
    )


# Backward-compatible private alias. Callers should use ``base_prompt``.
_base_prompt = base_prompt


def _build_repo_scope(sg_repo: str) -> str:
    """Render the ``repo_scope`` slot for the sourcegraph preamble.

    The new preamble assumes the workspace has no local source, so the
    scope statement is a single-line directive that names the indexed
    repository and shows the canonical scoping filter.
    """
    return (
        f"**Indexed repository:** `{sg_repo}`. Use `repo:^{sg_repo}$` "
        "in scoping filters to keep results focused on this repo."
    )


def _workflow_tail_default() -> str:
    """Default ``workflow_tail`` for change-scope-audit and unknown categories."""
    return (
        f"3. **Trace references** — Use `{_SOURCEGRAPH_FIND_REFERENCES}` on key "
        "symbols to discover indirect usages\n"
        "4. **Verify before denying existence** — If a Sourcegraph "
        "search returns no results for an identifier the question "
        "explicitly asks about, broaden the query (drop a term, try "
        f"stemmed forms via `{_SOURCEGRAPH_NLS_SEARCH}`, or browse "
        f"`{_SOURCEGRAPH_LIST_FILES}`) "
        "before concluding the identifier does not exist. Sourcegraph's "
        "index can lag the working tree on recent commits. Do not "
        "write a denial of existence based solely on a single empty "
        "search.\n"
        f"5. **Efficiency** — Don't read >15 files via `{_SOURCEGRAPH_READ_FILE}` "
        "without writing the answer. After 8 reads, integrate what you "
        "have and act. **Union results when recall matters** — combine "
        f"`{_SOURCEGRAPH_KEYWORD_SEARCH}` and `{_SOURCEGRAPH_NLS_SEARCH}` "
        "for broader coverage."
    )


def _workflow_tail_symbol_reference_trace() -> str:
    return (
        "3. **Authoritative references** — For symbol-reference-trace "
        f"tasks, treat `{_SOURCEGRAPH_FIND_REFERENCES}` as authoritative. It is the "
        "source of truth for symbol references; do not replace it with "
        "a keyword-search union and do not second-guess it with "
        f"`{_SOURCEGRAPH_DEEPSEARCH}`.\n"
        f"4. **Forbidden tools** — DO NOT call `{_SOURCEGRAPH_DEEPSEARCH}` or "
        f"`{_SOURCEGRAPH_DEEPSEARCH_READ}`. They are slow multi-step searches "
        f"that duplicate work `{_SOURCEGRAPH_FIND_REFERENCES}` already does "
        f"correctly. If `{_SOURCEGRAPH_FIND_REFERENCES}` returns a non-empty "
        f"set, write the answer; do not escalate to `{_SOURCEGRAPH_DEEPSEARCH}`.\n"
        "5. **Efficiency** — References rarely need broad reading. "
        f"Don't read >8 files via `{_SOURCEGRAPH_READ_FILE}` without writing "
        "the answer; after 4 reads, decide and respond."
    )


def _workflow_tail_oracle_checks() -> str:
    return (
        "3. **Coverage over depth** — The answer must address every "
        "declared criterion the rubric asks for. Before searching, "
        "list the explicit criteria the rubric asks for and enumerate "
        "them. Search only as deeply as needed to satisfy each one. "
        "Do not pursue tangential investigation depth at the cost of "
        "breadth across criteria.\n"
        "4. **Verify before denying existence** — The rubric guarantees "
        "the named symbol exists somewhere in the codebase. If "
        f"`{_SOURCEGRAPH_KEYWORD_SEARCH}` or `{_SOURCEGRAPH_FIND_REFERENCES}` "
        "returns no hits "
        "for an identifier the rubric explicitly asks about, broaden "
        f"the query via `{_SOURCEGRAPH_NLS_SEARCH}` (stemming) or "
        f"`{_SOURCEGRAPH_LIST_FILES}` "
        "(directory browse) before answering. Never write a denial "
        "of existence for a rubric-named symbol.\n"
        "5. **Coverage-first synthesis** — Don't read >12 files via "
        f"`{_SOURCEGRAPH_READ_FILE}` without producing answer text. After 6 reads, "
        "integrate what you have and write coverage for each "
        "criterion. Before finalising, re-read the criteria list and "
        "verify each is addressed. **Efficiency** caps each criterion "
        "at one authoritative reference."
    )


def _workflow_tail_sdlc() -> str:
    return (
        f"3. **Trace selectively** — Use `{_SOURCEGRAPH_FIND_REFERENCES}` only when "
        "a callsite or reference outside the named files is needed to "
        "make a correct edit. The instruction typically names the files "
        "to modify, so use Sourcegraph for additional references, "
        "NOT to pre-discover file lists. Implementation effort is the "
        "bottleneck, not navigation.\n"
        "4. **Stop searching when you have the file list and the "
        "references your edit needs** — Switch to writing code. Don't "
        "broaden scope unless the edit requires it.\n"
        f"5. **Efficiency** — Don't read >10 files via `{_SOURCEGRAPH_READ_FILE}` "
        "without editing. After 5 reads, write your edit and check it."
    )


_WORKFLOW_TAILS_BY_CATEGORY: dict[str, Callable[[], str]] = {}


def task_preamble_context(
    task_metadata: Mapping[str, object] | None,
    *,
    preamble_names: Iterable[str] | None = None,
    task_id: str = "",
    mcp_mode: str = "strict",
) -> dict[str, str]:
    """Extract task-aware context values that preamble templates can reference.

    When *preamble_names* contains a preamble that requires
    ``metadata.sg_repo`` (e.g. the ``sourcegraph`` preamble, whose template
    renders ``repo:^{{sg_repo}}$``), this function raises ``ValueError`` if
    ``metadata.sg_repo`` is missing or empty. That converts a silent runtime
    degradation (``repo:^$ <query>`` falling back to global search) into a
    clear failure at prompt-composition time. Callers that don't request a
    sg_repo-dependent preamble can omit ``preamble_names`` and the guard is a
    no-op.

    The function also populates two slots used by the v2 sourcegraph
    preamble (jf28):

    * ``repo_scope`` — single-line repository scoping directive.
    * ``workflow_tail`` — category-specialised continuation of the
      "Required Workflow" numbered list (steps 3+).
    * ``source_access_policy`` — strict/pragmatic/loose local-tool guidance.
    """
    if not isinstance(task_metadata, Mapping):
        return {}

    metadata_block = task_metadata.get("metadata")
    if not isinstance(metadata_block, Mapping):
        return {}

    extra_context: dict[str, str] = {}
    sg_repo = metadata_block.get("sg_repo")
    sg_repo_value = sg_repo if isinstance(sg_repo, str) else ""
    if sg_repo_value:
        extra_context["sg_repo"] = sg_repo_value

    requested = (
        frozenset(preamble_names) if preamble_names is not None else frozenset()
    )
    if requested & _SG_REPO_REQUIRED_PREAMBLES and not sg_repo_value:
        raise ValueError(
            f"sg_repo is empty for task {task_id!r}; cannot render "
            "Sourcegraph preamble. Re-mine the task with a populated origin "
            "remote or pass --sg-repo at mine time so metadata.sg_repo is "
            "set."
        )

    category = metadata_block.get("category")
    if isinstance(category, str) and category:
        extra_context["task_category"] = category

    # repo_scope only renders meaningfully when sg_repo is populated.
    # The guard above already fails when a sourcegraph preamble is
    # requested without sg_repo, so reaching this branch with an empty
    # sg_repo means a non-sourcegraph preamble — leave repo_scope unset
    # and the renderer will leave the {{repo_scope}} token intact.
    if sg_repo_value:
        extra_context["repo_scope"] = _build_repo_scope(sg_repo_value)

    tail_builder = _WORKFLOW_TAILS_BY_CATEGORY.get(
        extra_context.get("task_category", ""),
        _workflow_tail_default,
    )
    extra_context["workflow_tail"] = tail_builder()
    extra_context["source_access_policy"] = _source_access_policy(mcp_mode)

    return extra_context


def _source_access_policy(mcp_mode: str) -> str:
    mode = mcp_mode if mcp_mode in _MCP_MODES else "strict"
    if mode == "pragmatic":
        return (
            "Pragmatic MCP mode is active. `Read` is available for files that "
            "exist in the workspace. Use Sourcegraph MCP tools for search, "
            "reference tracing, definitions, remote reads, and cross-repo "
            "inspection. Built-in local search and shell tools are not part "
            "of this arm."
        )
    if mode == "loose":
        return (
            "Loose MCP mode is active. Local `Read`, `Grep`, `Glob`, and "
            "`Bash` may be available; use them only as explicit secondary "
            "checks after Sourcegraph MCP search or when the task requires "
            "workspace-specific files that the index cannot see."
        )
    return (
        "Strict MCP mode is active. Use Sourcegraph MCP tools for all "
        "repository inspection, search, reference tracing, definitions, and "
        "code reads. Built-in local file, search, and shell tools are not "
        "part of this arm. Use `Write` only to persist required outputs or "
        "edits."
    )


_WORKFLOW_TAILS_BY_CATEGORY.update(
    {
        _SYMBOL_REFERENCE_TRACE_CATEGORY: _workflow_tail_symbol_reference_trace,
        _ORACLE_CHECKS_CATEGORY: _workflow_tail_oracle_checks,
        _SDLC_CATEGORY: _workflow_tail_sdlc,
    }
)


def compose_instruction(
    instruction: str,
    repo_path: Path,
    preamble_names: list[str],
    resolver: PreambleResolver,
    task_id: str = "",
    *,
    worktree_path: Path | None = None,
    extra_context: dict[str, str] | None = None,
) -> tuple[str, list[dict[str, str]]]:
    """Build the full prompt from instruction + preamble blocks.

    Returns ``(prompt, resolved_preambles)`` where resolved_preambles is a
    list of dicts with ``name`` and ``content`` keys for reproducibility.

    *extra_context* is merged into the template context so preambles can
    reference task-specific values like ``{{sg_repo}}``.
    """
    base = base_prompt(instruction, repo_path, worktree_path=worktree_path)

    effective_path = worktree_path if worktree_path is not None else repo_path
    context = {
        "repo_path": str(effective_path),
        "repo_name": effective_path.name,
        "task_id": task_id,
    }
    if extra_context:
        context.update(extra_context)

    blocks = resolver.resolve(preamble_names)
    resolved: list[dict[str, str]] = []
    rendered_parts: list[str] = []

    for block in blocks:
        content = block.render(context)
        resolved.append({"name": block.name, "content": content})
        rendered_parts.append(content)

    prompt = base + "\n\n" + "\n\n".join(rendered_parts) if rendered_parts else base
    return prompt, resolved
