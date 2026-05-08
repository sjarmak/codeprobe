"""Class E fairness scanner — detect oracle leakage in agent-facing files.

Class E (from CSB Verification Report, 2026-04-24) is fairness drift caused by
files an AI agent reads *before* it sees a task's prompt: ``CLAUDE.md``,
``AGENTS.md``, ``README.md``, ``.cursor/rules/`` etc. If any of these files
mention literal oracle paths or symbols from a task in the corpus, agents that
read them get an unfair hint relative to agents that don't.

Two checks ship in this module:

1. **Static scan** — for every task under ``task_roots``, extract oracle
   tokens (file paths, symbols, scalar answers) from
   ``tests/ground_truth.json``. Then check every agent-facing file under
   ``repo_root`` for verbatim hits.
2. **Dynamic check** — given a set of pre-rendered preambles (typically from
   :func:`codeprobe.preambles.generator.render_preamble`), verify no oracle
   token from any task appears in any rendered preamble. The preamble is
   supposed to describe *which tools to use*, not the answer.

Token extraction reuses :func:`codeprobe.qa.verify._extract_oracle_tokens` so
the same token set is used for per-task leakage (Class D, already shipped) and
repo-level leakage (Class E, new). The leaf matcher is a copy of
:func:`codeprobe.qa.benchmark_qa_core.leakage.check_aux_file_leakage` adapted
to keep ``(task_id, token, file)`` triples for reporting.

ZFC: pure structural matching — regex word-boundary scan of literal tokens.
No semantic judgment.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from codeprobe.qa.verify import _extract_oracle_tokens, _read_ground_truth

__all__ = [
    "FairnessLeak",
    "FairnessResult",
    "check_fairness",
    "discover_agent_facing_files",
    "discover_task_dirs",
]

# Filename patterns recognised as agent-facing prompt files at the repo root.
# Any other file (CHANGELOG, LICENSE, contributor docs that aren't agent
# guidance) is excluded — those are read by humans, not by AI agents during
# task execution.
_AGENT_FACING_FILENAMES: frozenset[str] = frozenset(
    {
        "CLAUDE.md",
        "AGENTS.md",
        "AGENT.md",
        "README.md",
        "README.rst",
        "README.txt",
        "GEMINI.md",  # Google agents
        "COPILOT.md",  # GitHub Copilot
        ".cursorrules",  # legacy single-file Cursor rules
    }
)

# Subdirectories whose contents are agent-facing (Cursor rules, Aider config,
# Continue config, etc.). Files inside these dirs are scanned recursively.
_AGENT_FACING_DIRNAMES: frozenset[str] = frozenset(
    {
        ".cursor",
        ".aider",
        ".continue",
        ".github/copilot",  # nested — handled specially
    }
)

# Minimum token length below which leak findings are too noisy to be useful.
# Mirrors the ``F1`` threshold in benchmark_qa_core/leakage.py — short tokens
# (e.g. ``"3"``, ``"hi"``) match anywhere and would drown the report.
_MIN_TOKEN_LEN = 3

# Token shapes that are too generic to flag even at length >= 3. These are
# common English words or one-letter answers that produce noise without
# signal. The set is intentionally tiny — the cost of a false positive is a
# follow-up bead, not data corruption, so we err toward sensitivity.
_GENERIC_TOKEN_DENYLIST: frozenset[str] = frozenset(
    {
        "True",
        "False",
        "None",
        "true",
        "false",
        "null",
        "yes",
        "no",
        "Yes",
        "No",
    }
)


@dataclass(frozen=True)
class FairnessLeak:
    """One oracle-token hit inside an agent-facing file."""

    task_id: str
    """Stable identifier — task directory name, not the full path."""

    token: str
    """The oracle token that leaked."""

    location: str
    """File path (or rendered-preamble name) where the token appeared."""

    kind: str
    """``"static"`` for repo files, ``"preamble"`` for rendered preambles."""


@dataclass(frozen=True)
class FairnessResult:
    """Aggregate result of a fairness scan."""

    ok: bool
    """True iff zero leaks were detected across all checks."""

    tasks_scanned: int
    aux_files_scanned: int
    preambles_scanned: int
    leaks: list[FairnessLeak] = field(default_factory=list)

    @property
    def static_leaks(self) -> list[FairnessLeak]:
        return [leak for leak in self.leaks if leak.kind == "static"]

    @property
    def preamble_leaks(self) -> list[FairnessLeak]:
        return [leak for leak in self.leaks if leak.kind == "preamble"]

    def to_dict(self) -> dict:
        """JSON-serialisable summary for envelope output."""
        return {
            "ok": self.ok,
            "tasks_scanned": self.tasks_scanned,
            "aux_files_scanned": self.aux_files_scanned,
            "preambles_scanned": self.preambles_scanned,
            "leak_count": len(self.leaks),
            "static_leak_count": len(self.static_leaks),
            "preamble_leak_count": len(self.preamble_leaks),
            "leaks": [
                {
                    "task_id": leak.task_id,
                    "token": leak.token,
                    "location": leak.location,
                    "kind": leak.kind,
                }
                for leak in self.leaks
            ],
        }


def discover_task_dirs(corpus_roots: Iterable[Path]) -> list[Path]:
    """Find every task directory under the given corpus roots.

    A "task directory" is any directory containing either
    ``tests/ground_truth.json`` or a top-level ``ground_truth.json``. The
    walk is shallow-by-default — it recurses into subdirectories but treats
    a matched task dir as a leaf (no nested tasks).

    Worktree directories (``.claude/worktrees/``, ``.git/``) are skipped to
    avoid double-counting cloned task copies.
    """
    skip_dirs = {".git", "__pycache__", ".pytest_cache", ".mypy_cache",
                 ".ruff_cache", "node_modules"}
    skip_path_parts = {".claude/worktrees"}
    seen: set[Path] = set()
    found: list[Path] = []

    for root in corpus_roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for entry in _walk_tasks(root, skip_dirs, skip_path_parts):
            resolved = entry.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(entry)
    return sorted(found)


def _walk_tasks(
    root: Path,
    skip_dirs: set[str],
    skip_path_parts: set[str],
) -> Iterable[Path]:
    """Yield directories under ``root`` that look like tasks."""
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        if not current.is_dir():
            continue
        if current.name in skip_dirs:
            continue
        if any(part in str(current) for part in skip_path_parts):
            continue
        # A task dir contains either tests/ground_truth.json or
        # ground_truth.json directly.
        if (current / "tests" / "ground_truth.json").is_file() or (
            current / "ground_truth.json"
        ).is_file():
            yield current
            continue
        for child in current.iterdir():
            if child.is_dir():
                stack.append(child)


def discover_agent_facing_files(repo_root: Path) -> list[Path]:
    """Find every agent-facing prompt file inside ``repo_root``.

    Walks the tree and collects:

    - Filenames matching :data:`_AGENT_FACING_FILENAMES` (case-sensitive)
    - Files inside any directory matching :data:`_AGENT_FACING_DIRNAMES`

    Only includes files that exist and are regular files. Skips the same
    cache/worktree dirs as :func:`discover_task_dirs` so we don't pick up
    embedded copies of these files inside test fixtures or git worktrees.
    """
    repo_root = Path(repo_root)
    if not repo_root.is_dir():
        return []

    skip_dirs = {".git", "__pycache__", ".pytest_cache", ".mypy_cache",
                 ".ruff_cache", "node_modules", "dist", "build"}
    skip_path_parts = (".claude/worktrees",)

    found: list[Path] = []
    for path in repo_root.rglob("*"):
        if any(part in str(path) for part in skip_path_parts):
            continue
        if any(d in path.parts for d in skip_dirs):
            continue
        if not path.is_file():
            continue
        if path.name in _AGENT_FACING_FILENAMES:
            found.append(path)
            continue
        # Files under .cursor/, .aider/, .continue/ count as agent-facing.
        for parent in path.parents:
            if parent.name in _AGENT_FACING_DIRNAMES:
                found.append(path)
                break
    return sorted(set(found))


def check_fairness(
    task_roots: Sequence[Path],
    repo_root: Path,
    *,
    extra_agent_files: Sequence[Path] = (),
    rendered_preambles: Mapping[str, str] | None = None,
    skip_repo_walk: bool = False,
) -> FairnessResult:
    """Run the Class E fairness scan.

    Args:
        task_roots: Corpus directories to walk for task subdirectories. Each
            task is expected to contain ``tests/ground_truth.json`` (or a
            top-level ``ground_truth.json``).
        repo_root: Repository root whose agent-facing files should be
            scanned for oracle leaks. Pass the codeprobe repo root or the
            target benchmark repo.
        extra_agent_files: Additional explicit files to scan (e.g. a remote
            CLAUDE.md the harness injects at runtime). Each path is treated
            as agent-facing regardless of name.
        rendered_preambles: Optional ``{name: rendered_text}`` map of
            preambles to scan. Each preamble is matched against every
            task's oracle tokens; hits land in
            :attr:`FairnessResult.preamble_leaks`.
        skip_repo_walk: When True, do not auto-discover repo agent files
            via :func:`discover_agent_facing_files`. Useful for tests that
            want to scan only ``extra_agent_files``.

    Returns:
        A :class:`FairnessResult` summarising leaks per task and per file.
    """
    repo_root = Path(repo_root)

    task_dirs = discover_task_dirs(task_roots)

    if skip_repo_walk:
        agent_files: list[Path] = []
    else:
        agent_files = discover_agent_facing_files(repo_root)
    agent_files = sorted(set(agent_files) | {Path(p) for p in extra_agent_files})

    # Build {task_id: tokens} once so we can reuse for static + preamble checks.
    token_map: dict[str, list[str]] = {}
    for task_dir in task_dirs:
        gt = _read_ground_truth(task_dir)
        tokens = _extract_oracle_tokens(gt)
        # Apply Class E filtering: drop tokens too short to be specific or
        # too generic to be diagnostic.
        filtered = [
            tok
            for tok in tokens
            if len(tok) >= _MIN_TOKEN_LEN and tok not in _GENERIC_TOKEN_DENYLIST
        ]
        if filtered:
            token_map[task_dir.name] = filtered

    leaks: list[FairnessLeak] = []

    # Static scan: for every (task, token), search every agent-facing file.
    file_text_cache: dict[Path, str | None] = {}
    for task_id, tokens in token_map.items():
        for token in tokens:
            pattern = re.compile(rf"\b{re.escape(token)}\b")
            for aux in agent_files:
                text = _read_text_cached(aux, file_text_cache)
                if text is None:
                    continue
                if pattern.search(text):
                    leaks.append(
                        FairnessLeak(
                            task_id=task_id,
                            token=token,
                            location=str(aux),
                            kind="static",
                        )
                    )

    # Dynamic preamble scan.
    if rendered_preambles:
        for preamble_name, preamble_text in rendered_preambles.items():
            for task_id, tokens in token_map.items():
                for token in tokens:
                    pattern = re.compile(rf"\b{re.escape(token)}\b")
                    if pattern.search(preamble_text):
                        leaks.append(
                            FairnessLeak(
                                task_id=task_id,
                                token=token,
                                location=preamble_name,
                                kind="preamble",
                            )
                        )

    return FairnessResult(
        ok=not leaks,
        tasks_scanned=len(task_dirs),
        aux_files_scanned=len(agent_files),
        preambles_scanned=len(rendered_preambles or {}),
        leaks=leaks,
    )


def _read_text_cached(path: Path, cache: dict[Path, str | None]) -> str | None:
    """Read ``path`` once, caching the result. Returns None on read failure."""
    if path in cache:
        return cache[path]
    if not path.is_file():
        cache[path] = None
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        cache[path] = None
        return None
    cache[path] = text
    return text


def write_fairness_report(result: FairnessResult, out_path: Path) -> None:
    """Write a JSON report for downstream consumption (CI gate, archives)."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
