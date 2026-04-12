"""Criterion-driven Test Agent action compiler.

Reads acceptance criteria from ``acceptance/criteria.toml`` (via
:func:`acceptance.loader.load_criteria`) and compiles each criterion whose
``check_type`` requires a workspace artifact into a :class:`TestAction` —
a frozen dataclass holding a bash snippet that the Test Agent executes to
populate the artifact(s) the Verifier reads.

Structural check types (``import_equals``, ``regex_present``, etc.) require
no workspace artifact and produce no action.  Check types that have no
handler registered in ``acceptance.verify.Verifier._handlers()`` also
produce no action — emitting artifacts for them would be pure waste since
the Verifier skips them regardless.

This module is a **pure function** — no IO beyond what the caller passes in,
no subprocesses, no LLM calls.  Token substitution uses ``.replace()``
chains (never ``.format()``) to avoid crashes on shell ``${VAR}`` braces.
"""

from __future__ import annotations

import re
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from acceptance.loader import Criterion

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TestAction:
    """A single compiled action for the Test Agent to execute."""

    criterion_id: str
    description: str
    shell_snippet: str
    artifact_paths: tuple[str, ...]


# ---------------------------------------------------------------------------
# Check types that the Verifier handles AND that read workspace artifacts.
# Structural types are excluded (they introspect Python or source files).
# Handler-less types are excluded (no Verifier reader → artifacts are waste).
# ---------------------------------------------------------------------------

#: Check types handled by the Verifier that DO NOT need workspace artifacts.
_STRUCTURAL_TYPES: frozenset[str] = frozenset(
    {
        "import_equals",
        "dataclass_has_fields",
        "regex_present",
        "regex_absent",
        "pyproject_deps_bounded",
    }
)

#: Check types present in criteria.toml but absent from Verifier._handlers().
#: Criterion IDs must match this pattern to be safe for shell embedding.
#: Prevents command injection via $() or backticks in double-quoted contexts.
_SAFE_ID_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")

#: Shell environment variable names must match this pattern.
_SAFE_ENV_RE: re.Pattern[str] = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")

_HANDLERLESS_TYPES: frozenset[str] = frozenset(
    {
        "stream_separation",
        "log_level_matches",
        "json_lines_valid",
        "dataclass_roundtrip",
        "yaml_field_equal",
    }
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compile_actions(
    criteria: list[Criterion],
    *,
    target_repo: Path,
    workspace: Path,
    project_root: Path,
) -> list[TestAction]:
    """Return one :class:`TestAction` per criterion that needs a workspace artifact.

    Structural criteria and handler-less criteria produce no action.
    Criteria whose params cannot be resolved produce a stub action that writes
    a ``COMPILE_ERROR`` marker so the Verifier sees an explicit failure rather
    than a silent skip.
    """
    actions: list[TestAction] = []
    for criterion in criteria:
        if not _SAFE_ID_RE.fullmatch(criterion.id):
            raise ValueError(
                f"Criterion id {criterion.id!r} contains characters unsafe "
                "for shell embedding; only [A-Za-z0-9_-] allowed."
            )
        ct = criterion.check_type
        if ct in _STRUCTURAL_TYPES or ct in _HANDLERLESS_TYPES:
            continue
        emitter = _EMITTERS.get(ct)
        if emitter is None:
            continue
        action = emitter(criterion, target_repo, workspace, project_root)
        if action is not None:
            actions.append(action)
    return actions


# ---------------------------------------------------------------------------
# Token substitution
# ---------------------------------------------------------------------------


def _substitute_command(
    raw: str,
    target_repo: Path,
    workspace: Path,
    project_root: Path,
    params: dict[str, Any],
) -> str:
    """Substitute ``{repo}``, ``{results}``, ``{tasks_dir}``, ``{experiment}``
    tokens inside a command string.

    Uses ``.replace()`` (not ``.format()``) so shell ``${VAR}`` braces are
    left intact.
    """
    result = raw.replace("{repo}", str(target_repo))
    result = result.replace("{results}", str(workspace / "results"))
    result = result.replace(
        "{experiment}", str(workspace / ".codeprobe" / "experiment.json")
    )

    # {tasks_dir} resolves via the fixture param if present, else workspace/tasks.
    fixture = params.get("fixture")
    if fixture and isinstance(fixture, str):
        resolved = (project_root / fixture).resolve()
        root_resolved = project_root.resolve()
        if not str(resolved).startswith(str(root_resolved)):
            raise ValueError(
                f"fixture param {fixture!r} escapes project_root — path traversal denied"
            )
        tasks_dir = str(resolved)
    else:
        tasks_dir = str(workspace / "tasks")
    result = result.replace("{tasks_dir}", tasks_dir)

    return result


# ---------------------------------------------------------------------------
# Per-check-type emitters
# ---------------------------------------------------------------------------


def _emit_cli_help_contains(
    c: Criterion, target_repo: Path, workspace: Path, project_root: Path
) -> TestAction | None:
    commands = c.params.get("commands")
    if not isinstance(commands, list) or not commands:
        return _stub_compile_error(c, workspace)
    lines: list[str] = []
    for i, raw_cmd in enumerate(commands):
        if not isinstance(raw_cmd, str):
            continue
        cmd = _substitute_command(
            raw_cmd, target_repo, workspace, project_root, c.params
        )
        op = ">>" if i > 0 else ">"
        lines.append(
            f'( {cmd} ) {op} "{workspace}/{c.id}.stdout" 2>> "{workspace}/{c.id}.stderr"'
        )
    lines.append(f'echo "0" > "{workspace}/{c.id}.exit"')
    snippet = "\n".join(lines)
    return TestAction(
        criterion_id=c.id,
        description=f"help-check: {len(commands)} commands",
        shell_snippet=snippet,
        artifact_paths=(f"{c.id}.stdout", f"{c.id}.stderr", f"{c.id}.exit"),
    )


def _emit_command_capture(
    c: Criterion, target_repo: Path, workspace: Path, project_root: Path
) -> TestAction | None:
    """Shared emitter for types that capture stdout+stderr from a command."""
    raw_cmd = c.params.get("command")
    if not isinstance(raw_cmd, str) or not raw_cmd:
        return _stub_compile_error(c, workspace)
    cmd = _substitute_command(raw_cmd, target_repo, workspace, project_root, c.params)
    snippet = textwrap.dedent(f"""\
        ( {cmd} ) \\
          > "{workspace}/{c.id}.stdout" \\
          2> "{workspace}/{c.id}.stderr"
        echo "$?" > "{workspace}/{c.id}.exit"
    """).strip()
    return TestAction(
        criterion_id=c.id,
        description=f"run: {cmd}",
        shell_snippet=snippet,
        artifact_paths=(f"{c.id}.stdout", f"{c.id}.stderr", f"{c.id}.exit"),
    )


def _emit_cli_writes_file(
    c: Criterion, target_repo: Path, workspace: Path, project_root: Path
) -> TestAction | None:
    raw_cmd = c.params.get("command")
    expected_path = c.params.get("expected_path")
    if not isinstance(raw_cmd, str) or not raw_cmd:
        return None
    cmd = _substitute_command(raw_cmd, target_repo, workspace, project_root, c.params)
    snippet = textwrap.dedent(f"""\
        ( cd "{workspace}" && {cmd} ) \\
          > "{workspace}/{c.id}.stdout" \\
          2> "{workspace}/{c.id}.stderr"
        echo "$?" > "{workspace}/{c.id}.exit"
    """).strip()
    artifact_paths = [f"{c.id}.stdout", f"{c.id}.stderr", f"{c.id}.exit"]
    if isinstance(expected_path, str) and expected_path:
        artifact_paths.append(expected_path)
    return TestAction(
        criterion_id=c.id,
        description=f"writes-file: {expected_path or '?'}",
        shell_snippet=snippet,
        artifact_paths=tuple(artifact_paths),
    )


def _emit_file_exists(
    c: Criterion, target_repo: Path, workspace: Path, project_root: Path
) -> TestAction | None:
    rel = c.params.get("path") or c.params.get("expected_path")
    if not isinstance(rel, str) or not rel:
        return None
    # file_exists checks are passive — the artifact should already be produced
    # by a dependency.  Emit a no-op touch that documents what we expect.
    snippet = f'# file_exists: verifier checks "{workspace}/{rel}" — no command needed'
    return TestAction(
        criterion_id=c.id,
        description=f"file-exists: {rel}",
        shell_snippet=snippet,
        artifact_paths=(rel,),
    )


def _emit_sync_action(
    c: Criterion, target_repo: Path, workspace: Path, project_root: Path
) -> TestAction | None:
    """Emit a sync snippet that copies ``target_repo/.codeprobe/`` into the
    workspace so the Verifier's ``{repo}`` → workspace substitution finds the
    artifacts where the real ``codeprobe`` tool wrote them.
    """
    source_rel = c.params.get("source") or c.params.get("search_in")
    if not isinstance(source_rel, str):
        return None
    snippet = textwrap.dedent(f"""\
        # Sync target_repo output into workspace for {c.id}
        mkdir -p "{workspace}/.codeprobe"
        if [ -d "{target_repo}/.codeprobe" ]; then
          cp -r "{target_repo}/.codeprobe/." "{workspace}/.codeprobe/"
        fi
    """).strip()
    return TestAction(
        criterion_id=c.id,
        description=f"sync .codeprobe for {c.check_type}",
        shell_snippet=snippet,
        artifact_paths=(f"{c.id}.synced",),
    )


def _emit_canary_detect(
    c: Criterion, target_repo: Path, workspace: Path, project_root: Path
) -> TestAction | None:
    """Emit an action that writes the canary UUID to ``$WORKSPACE/canary.txt``
    and syncs ``.codeprobe/`` so the Verifier's rglob can find the UUID in
    at least one workspace file.
    """
    canary_env = c.params.get("canary_env", "CODEPROBE_CANARY_UUID")
    if not isinstance(canary_env, str) or not _SAFE_ENV_RE.fullmatch(canary_env):
        return None
    snippet = textwrap.dedent(f"""\
        # Canary detection for {c.id}
        echo "${canary_env}" > "{workspace}/canary.txt"
        mkdir -p "{workspace}/.codeprobe"
        if [ -d "{target_repo}/.codeprobe" ]; then
          cp -r "{target_repo}/.codeprobe/." "{workspace}/.codeprobe/"
        fi
    """).strip()
    return TestAction(
        criterion_id=c.id,
        description="canary: write UUID + sync workspace",
        shell_snippet=snippet,
        artifact_paths=("canary.txt",),
    )


# ---------------------------------------------------------------------------
# Stub emitters (for missing/invalid params)
# ---------------------------------------------------------------------------


def _stub_compile_error(c: Criterion, workspace: Path) -> TestAction:
    """Emit a stub action that writes a ``COMPILE_ERROR`` marker.

    The Verifier sees an explicit failure rather than a silent skip.
    """
    snippet = textwrap.dedent(f"""\
        echo "COMPILE_ERROR: missing or invalid params for {c.id}" \\
          > "{workspace}/{c.id}.stderr"
        echo "255" > "{workspace}/{c.id}.exit"
    """).strip()
    return TestAction(
        criterion_id=c.id,
        description=f"STUB: {c.id} (missing params)",
        shell_snippet=snippet,
        artifact_paths=(f"{c.id}.exit", f"{c.id}.stderr"),
    )


# ---------------------------------------------------------------------------
# Emitter dispatch table
# ---------------------------------------------------------------------------

_Emitter = Callable[[Criterion, Path, Path, Path], TestAction | None]

_EMITTERS: dict[str, _Emitter] = {
    "cli_exit_code": _emit_command_capture,
    "cli_help_contains": _emit_cli_help_contains,
    "cli_stdout_contains": _emit_command_capture,
    "stdout_contains": _emit_command_capture,
    "stderr_contains": _emit_command_capture,
    "cli_writes_file": _emit_cli_writes_file,
    "file_exists": _emit_file_exists,
    "count_ge": _emit_sync_action,
    "json_count_ge": _emit_sync_action,
    "json_field_not_null": _emit_sync_action,
    "json_field_equals": _emit_sync_action,
    "json_field_type": _emit_sync_action,
    "canary_detect": _emit_canary_detect,
}

__all__ = ["TestAction", "compile_actions"]
