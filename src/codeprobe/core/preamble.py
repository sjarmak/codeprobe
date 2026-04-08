"""Preamble resolution and instruction composition."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from codeprobe.models.preamble import PreambleBlock
from codeprobe.preambles import get_builtin


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


def _base_prompt(
    instruction: str, repo_path: Path, *, worktree_path: Path | None = None
) -> str:
    """Build the base prompt wrapper shared across prompt-building paths.

    When *worktree_path* is provided (parallel isolation mode), the prompt
    references the worktree instead of the original repo path.
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
    base = _base_prompt(instruction, repo_path, worktree_path=worktree_path)

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
