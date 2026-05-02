"""Preamble resolution and instruction composition."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Protocol, runtime_checkable

from codeprobe.models.preamble import PreambleBlock
from codeprobe.preambles import get_builtin

__all__ = [
    "PreambleResolver",
    "DefaultPreambleResolver",
    "base_prompt",
    "compose_instruction",
    "task_preamble_context",
]

_SYMBOL_REFERENCE_TRACE_CATEGORY = "symbol-reference-trace"
_ORACLE_CHECKS_CATEGORY = "oracle_checks"
_SDLC_CATEGORY = "sdlc"

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
    ) -> None:
        self._search_dirs: list[Path] = []

        # Task-local (highest priority)
        self._search_dirs.append(task_dir / "preambles")

        # Project-level
        if project_dir is not None:
            self._search_dirs.append(project_dir / ".codeprobe" / "preambles")

        # User-level
        if user_dir is not None:
            self._search_dirs.append(user_dir / ".codeprobe" / "preambles")

    def resolve(self, names: list[str]) -> list[PreambleBlock]:
        """Resolve each name by searching directories in priority order."""
        return [self._resolve_one(name) for name in names]

    def _resolve_one(self, name: str) -> PreambleBlock:
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


def task_preamble_context(
    task_metadata: Mapping[str, object] | None,
    *,
    preamble_names: Iterable[str] | None = None,
    task_id: str = "",
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

    if extra_context.get("task_category") == _SYMBOL_REFERENCE_TRACE_CATEGORY:
        extra_context.update(
            {
                "sg_task_search_guidance": (
                    "For `symbol-reference-trace`, treat "
                    "`sg_find_references` as authoritative. Use local Grep "
                    "only to locate the definition site or sanity-check a "
                    "suspicious gap. **DO NOT call `sg_deepsearch` or "
                    "`sg_deepsearch_read`** — they are slow multi-step "
                    "searches that duplicate work `sg_find_references` "
                    "already does correctly."
                ),
                "sg_find_references_guidance": (
                    "For `symbol-reference-trace`, this is the source of "
                    "truth; do not replace it with a grep union and do not "
                    "second-guess it with `sg_deepsearch`."
                ),
                "sg_local_search_step": (
                    "**Use local Grep narrowly** only when you need to find "
                    "the definition site or investigate a mismatch."
                ),
                "sg_negative_result_handling": (
                    "If `sg_find_references` returns an empty set for the "
                    "target symbol, **verify with a local `Grep` over the "
                    "working tree before reporting 'no references'**. "
                    "Sourcegraph's index can lag the working tree on recent "
                    "commits."
                ),
                "sg_result_synthesis_step": (
                    "**Efficiency** — references rarely need broad reading. "
                    "Don't read >8 files via `sg_read_file` without writing "
                    "the answer; after 4 reads, decide and respond. "
                    "**Prefer authoritative references** — report the "
                    "`sg_find_references` result set instead of unioning in "
                    "extra grep matches. If `sg_find_references` returns a "
                    "non-empty set, write the answer; do not escalate to "
                    "`sg_deepsearch`."
                ),
            }
        )
        return extra_context

    if extra_context.get("task_category") == _ORACLE_CHECKS_CATEGORY:
        extra_context.update(
            {
                "sg_task_search_guidance": (
                    "For `oracle_checks` rubric questions, the answer must "
                    "address every declared criterion. Before searching, "
                    "list the explicit criteria the question asks for "
                    "(typically named in the rubric or mentioned by name in "
                    "the prompt). Search only as deeply as needed to "
                    "satisfy each criterion. **Do not pursue tangential "
                    "investigation depth at the cost of breadth across "
                    "criteria.**"
                ),
                "sg_find_references_guidance": (
                    "Use this when a criterion explicitly asks about a "
                    "symbol's relationships. Stop when you have one "
                    "authoritative reference per criterion that asks for "
                    "one."
                ),
                "sg_local_search_step": (
                    "**Use local Grep narrowly** to confirm a specific "
                    "identifier or path is mentioned in the criterion's "
                    "required content."
                ),
                "sg_negative_result_handling": (
                    "**Verify before denying existence.** The rubric "
                    "guarantees the named symbol exists somewhere in the "
                    "codebase. If `sg_keyword_search` or "
                    "`sg_find_references` returns no hits for an identifier "
                    "the rubric explicitly asks about, **run a local "
                    "`Grep` over the working tree before answering**. "
                    "Sourcegraph's index can lag the working tree, "
                    "particularly for recent commits. **Never write a "
                    "denial of existence** for a rubric-named symbol — if "
                    "Sourcegraph misses it, fall back to local Grep before "
                    "concluding it does not exist."
                ),
                "sg_result_synthesis_step": (
                    "**Efficiency** — don't read >12 files via "
                    "`sg_read_file` without producing answer text. After 6 "
                    "reads, integrate what you have and write coverage for "
                    "each criterion. **Coverage-first synthesis** — before "
                    "finalizing your response, re-read the criteria list "
                    "and verify each is addressed. If any criterion isn't, "
                    "search specifically for what it asks (including a "
                    "local Grep fallback if Sourcegraph returned empty). "
                    "Otherwise, write the answer."
                ),
            }
        )
        return extra_context

    if extra_context.get("task_category") == _SDLC_CATEGORY:
        extra_context.update(
            {
                "sg_task_search_guidance": (
                    "For SDLC implementation tasks, the instruction "
                    "typically names the files to modify. Use Sourcegraph "
                    "for *additional* references when a symbol's full "
                    "usage matters for your edit, NOT to pre-discover file "
                    "lists. Implementation effort is the bottleneck, not "
                    "navigation."
                ),
                "sg_find_references_guidance": (
                    "Use this only when a callsite or reference outside "
                    "the named files is needed to make a correct edit. "
                    "Otherwise skip."
                ),
                "sg_local_search_step": (
                    "**Use local Grep narrowly** to find usages within or "
                    "adjacent to the named files. Don't broaden scope "
                    "unless the edit requires it."
                ),
                "sg_negative_result_handling": (
                    "If a Sourcegraph search misses an identifier you "
                    "expect to exist (e.g., a recently-added symbol), "
                    "**fall back to local `Grep` rather than blocking on "
                    "the index**. Implementation effort, not navigation, "
                    "is the bottleneck."
                ),
                "sg_result_synthesis_step": (
                    "**Efficiency** — don't read >10 files via "
                    "`sg_read_file` without editing. After 5 reads, write "
                    "your edit and check it. "
                    "**Stop searching when you have the file list and the "
                    "references your edit needs.** Switch to writing code. "
                    "Don't unionize — implementation effort dominates the "
                    "trial budget."
                ),
            }
        )
        return extra_context

    extra_context.update(
        {
            "sg_task_search_guidance": (
                "Use Sourcegraph tools FIRST, then supplement with local "
                "Grep when broader recall helps."
            ),
            "sg_find_references_guidance": (
                "For symbol-relationship questions, prefer this over "
                "heuristic text matches."
            ),
            "sg_local_search_step": (
                "**Supplement with local Grep** to catch anything Sourcegraph "
                "may have missed."
            ),
            "sg_negative_result_handling": (
                "**Verify before denying existence.** If a Sourcegraph "
                "search returns no results for an identifier the question "
                "explicitly asks about, **run a local `Grep` over the "
                "working tree before concluding the identifier does not "
                "exist**. Sourcegraph's index can lag the working tree, "
                "particularly for recent commits. **Do not write a denial "
                "of existence** based solely on a Sourcegraph negative."
            ),
            "sg_result_synthesis_step": (
                "**Efficiency** — don't read >15 files via `sg_read_file` "
                "without writing code or answer text. After 8 reads, "
                "integrate what you have and act. "
                "**Union results when recall matters** — combine indexed "
                "search and local Grep for broader coverage."
            ),
        }
    )
    return extra_context


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
