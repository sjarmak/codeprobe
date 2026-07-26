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
no subprocesses, no LLM calls.  A token-bearing command template must satisfy
a conservative, single-line, unquoted shell-word grammar (see
:func:`_substitute_command`); templates that embed a shell substitution or
expansion scope (``$(``, backtick, ``${``, ``$[``) are rejected, and each
accepted token value is ``shlex.quote``-d before substitution (CWE-78). A
template with no path token is passed through verbatim.
"""

from __future__ import annotations

import re
import shlex
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
#: ``dataclass_roundtrip`` and ``yaml_field_equal`` join this set: like the
#: other structural checks, their Verifier handlers read project files
#: (a JSON fixture, workflow YAML) directly, so no Test Agent artifact is
#: emitted for them.
_STRUCTURAL_TYPES: frozenset[str] = frozenset(
    {
        "import_equals",
        "dataclass_has_fields",
        "dataclass_roundtrip",
        "regex_present",
        "regex_absent",
        "pyproject_deps_bounded",
        "yaml_field_equal",
    }
)

#: Check types present in criteria.toml but absent from Verifier._handlers().
#: Criterion IDs must match this pattern to be safe for shell embedding.
#: Prevents command injection via $() or backticks in double-quoted contexts.
_SAFE_ID_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")

#: Shell environment variable names must match this pattern.
_SAFE_ENV_RE: re.Pattern[str] = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")

#: Check types present in criteria.toml but absent from Verifier._handlers().
#: ``log_level_matches`` remains here: a subprocess's effective logger level
#: is not observable from ``--help`` (help exits before the CLI callback
#: configures logging), so no honest behavioral handler exists yet — the
#: criterion stays a ``no_handler`` skip, excluded from the denominator,
#: until its contract is repointed at an observable signal (codeprobe-2s54).
_HANDLERLESS_TYPES: frozenset[str] = frozenset(
    {
        "log_level_matches",
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


#: The path tokens a criterion command may embed. Matched and substituted in a
#: single pass so an inserted value is never rescanned (see _substitute_command).
_PATH_TOKEN_RE: re.Pattern[str] = re.compile(
    r"\{(repo|workspace|results|experiment|tasks_dir)\}"
)

#: Characters allowed in a path suffix that immediately follows a token
#: (``{workspace}/log-stderr-probes``). Every character is shell-safe — no
#: metacharacter, quote, or whitespace — so the suffix can abut the quoted
#: token value without further quoting.
_SAFE_SUFFIX_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9._/@%+=:,-]*")


def _resolve_tasks_dir(
    workspace: Path, project_root: Path, params: dict[str, Any]
) -> str:
    """Return the unquoted ``{tasks_dir}`` value.

    With a ``fixture`` param it is that fixture resolved under ``project_root``
    (rejected if it escapes, via a path-aware containment check — a naive
    ``str.startswith`` accepts a sibling that merely shares a name prefix, e.g.
    ``project`` vs ``project-evil``); otherwise it is ``workspace/tasks``.
    """
    fixture = params.get("fixture")
    if fixture and isinstance(fixture, str):
        resolved = (project_root / fixture).resolve()
        if not resolved.is_relative_to(project_root.resolve()):
            raise ValueError(
                f"fixture param {fixture!r} escapes project_root — path traversal denied"
            )
        return str(resolved)
    return str(workspace / "tasks")


#: Shell scopes that could reparse or nest a substituted value. Any occurrence
#: in a token-bearing template is rejected so the flat quote-state grammar stays
#: valid: command substitution ``$(`` (also covers arithmetic ``$((``), backtick
#: command substitution, parameter expansion ``${``, and legacy arithmetic
#: ``$[``. Plain ``$VAR`` (no scope) does not reparse a value and is allowed.
_REPARSE_SCOPES: tuple[str, ...] = ("$(", "${", "$[", "`")


def _reject_reparsing_template(raw: str) -> None:
    """Reject a token-bearing template that a token value could not be made safe
    inside: a multiline (heredoc) form, or any reparsing/expansion scope
    (:data:`_REPARSE_SCOPES`). Templates with no path token are unaffected.
    """
    if _PATH_TOKEN_RE.search(raw) is None:
        return
    if "\n" in raw or "\r" in raw:
        raise ValueError(
            "path-token command templates must be single-line unquoted shell-word "
            "forms; a token inside a multiline template (e.g. a heredoc body) is "
            "not a shell word and its shlex.quote guard would become literal text "
            "while expansion still ran — rewrite as a single line."
        )
    scope = next((s for s in _REPARSE_SCOPES if s in raw), None)
    if scope is not None:
        raise ValueError(
            f"path-token command templates must not contain the shell substitution "
            f"or expansion scope {scope!r} (command substitution '$(' or backtick, "
            "parameter expansion '${', arithmetic '$((' or '$[') — a token value "
            "must never share a template with a construct that could reparse it."
        )


def _token_values(
    target_repo: Path, workspace: Path, project_root: Path, params: dict[str, Any]
) -> dict[str, str]:
    """Resolve the unquoted value each path token substitutes to."""
    return {
        "repo": str(target_repo),
        "workspace": str(workspace),
        "results": str(workspace / "results"),
        "experiment": str(workspace / ".codeprobe" / "experiment.json"),
        "tasks_dir": _resolve_tasks_dir(workspace, project_root, params),
    }


def _accept_token_word(raw: str, start: int, end: int, quote: str | None) -> tuple[str, int]:
    """Validate a token match at ``[start, end)`` is an unquoted standalone
    shell-word (optionally with a ``/suffix``) and return ``(suffix, next_i)``.

    Raises ``ValueError`` if the token is inside a quote context, does not begin
    a shell-word (whitespace/start before it), or ends at anything other than a
    word boundary or a ``/suffix`` of shell-safe characters.
    """
    token = raw[start:end]
    if quote is not None:
        raise ValueError(
            f"path token {token!r} appears inside a {quote!r} quoted context; "
            "tokens are unquoted shell-word placeholders (shlex-quoted on "
            "substitution) — remove the surrounding quotes"
        )
    if start != 0 and raw[start - 1] not in " \t":
        raise ValueError(
            f"path token {token!r} must begin a shell-word (start of the command "
            f"or whitespace before it), not follow {raw[start - 1]!r}; tokens are "
            "unquoted shell-word placeholders"
        )
    n = len(raw)
    if end >= n or raw[end] in " \t":
        return "", end
    if raw[end] != "/":
        raise ValueError(
            f"path token {token!r} must end at a word boundary or a '/suffix', "
            f"not {raw[end]!r}"
        )
    sfx = _SAFE_SUFFIX_RE.match(raw, end)
    sfx_end = sfx.end() if sfx is not None else end
    if sfx_end < n and raw[sfx_end] not in " \t":
        raise ValueError(
            f"path token {token!r} suffix has an unsafe character {raw[sfx_end]!r}; "
            "only [A-Za-z0-9._/@%+=:,-] are allowed"
        )
    return raw[end:sfx_end], sfx_end


def _substitute_command(
    raw: str,
    target_repo: Path,
    workspace: Path,
    project_root: Path,
    params: dict[str, Any],
) -> str:
    """Substitute path tokens in a command string, safely (CWE-78).

    Rejects any reparsing/expansion scope in a token-bearing template
    (:func:`_reject_reparsing_template`); requires each token to be an unquoted
    standalone shell-word, optionally with a ``/suffix``
    (:func:`_accept_token_word`); ``shlex.quote``-s each value and substitutes in
    one left-to-right pass, so a value containing another token's literal text is
    never rescanned. A quote-state machine (escaped-quote aware) rejects a token
    inside quotes even when whitespace-bounded. Deliberate reparsers (``eval
    {repo}``) are out of scope for these trusted, human-reviewed templates.
    """
    _reject_reparsing_template(raw)
    values = _token_values(target_repo, workspace, project_root, params)
    out: list[str] = []
    quote: str | None = None  # active quote context: None | "'" | '"'
    i = 0
    n = len(raw)
    while i < n:
        match = _PATH_TOKEN_RE.match(raw, i)
        if match is not None:
            suffix, i = _accept_token_word(raw, i, match.end(), quote)
            out.append(shlex.quote(values[match.group(1)]))
            out.append(suffix)
            continue
        ch = raw[i]
        # Outside single quotes a backslash escapes the next char (so \" cannot
        # toggle quote state); inside single quotes it is literal.
        if ch == "\\" and quote != "'" and i + 1 < n:
            out.append(ch)
            out.append(raw[i + 1])
            i += 2
            continue
        if quote is None and ch in ("'", '"'):
            quote = ch
        elif quote == ch:
            quote = None
        out.append(ch)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Per-check-type emitters
# ---------------------------------------------------------------------------


def _q(value: object) -> str:
    """Shell-quote a value for safe embedding as a single argument (CWE-78).

    Every generated filesystem path — redirection targets, ``mkdir``/``cp``/
    ``cd``/``touch`` arguments — is quoted with this. Double quotes are NOT
    enough: ``"$(...)"`` still runs a command substitution, so a workspace or
    target-repo path whose name contains ``$(...)`` would execute. ``shlex.quote``
    emits a single-quoted, inert argument instead.
    """
    return shlex.quote(str(value))


def _emit_cli_help_contains(
    c: Criterion, target_repo: Path, workspace: Path, project_root: Path
) -> TestAction | None:
    commands = c.params.get("commands")
    if not isinstance(commands, list) or not commands:
        return _stub_compile_error(c, workspace)
    stdout_p = _q(workspace / f"{c.id}.stdout")
    stderr_p = _q(workspace / f"{c.id}.stderr")
    exit_p = _q(workspace / f"{c.id}.exit")
    lines: list[str] = []
    for i, raw_cmd in enumerate(commands):
        if not isinstance(raw_cmd, str):
            continue
        cmd = _substitute_command(
            raw_cmd, target_repo, workspace, project_root, c.params
        )
        op = ">>" if i > 0 else ">"
        lines.append(f"( {cmd} ) {op} {stdout_p} 2>> {stderr_p}")
    lines.append(f"echo 0 > {exit_p}")
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
          > {_q(workspace / f"{c.id}.stdout")} \\
          2> {_q(workspace / f"{c.id}.stderr")}
        echo "$?" > {_q(workspace / f"{c.id}.exit")}
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
        ( cd {_q(workspace)} && {cmd} ) \\
          > {_q(workspace / f"{c.id}.stdout")} \\
          2> {_q(workspace / f"{c.id}.stderr")}
        echo "$?" > {_q(workspace / f"{c.id}.exit")}
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
    # by a dependency.  Emit a no-op comment (no embedded path, so a rel value
    # containing a newline cannot smuggle a second, executable line).
    snippet = f"# file_exists: verifier checks the {c.id} artifact — no command needed"
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
    ws_codeprobe = _q(workspace / ".codeprobe")
    tr_codeprobe = _q(target_repo / ".codeprobe")
    tr_codeprobe_contents = _q(f"{target_repo / '.codeprobe'}/.")
    ws_codeprobe_dest = _q(f"{workspace / '.codeprobe'}/")
    snippet = textwrap.dedent(f"""\
        # Sync target_repo output into workspace for {c.id}
        mkdir -p {ws_codeprobe}
        if [ -d {tr_codeprobe} ]; then
          cp -r {tr_codeprobe_contents} {ws_codeprobe_dest}
        fi
        touch {_q(workspace / f"{c.id}.synced")}
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
    ws_codeprobe = _q(workspace / ".codeprobe")
    tr_codeprobe = _q(target_repo / ".codeprobe")
    tr_codeprobe_contents = _q(f"{target_repo / '.codeprobe'}/.")
    ws_codeprobe_dest = _q(f"{workspace / '.codeprobe'}/")
    snippet = textwrap.dedent(f"""\
        # Canary detection for {c.id}
        echo "${canary_env}" > {_q(workspace / "canary.txt")}
        mkdir -p {ws_codeprobe}
        if [ -d {tr_codeprobe} ]; then
          cp -r {tr_codeprobe_contents} {ws_codeprobe_dest}
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
          > {_q(workspace / f"{c.id}.stdout")}
        echo "COMPILE_ERROR: missing or invalid params for {c.id}" \\
          > {_q(workspace / f"{c.id}.stderr")}
        echo "255" > {_q(workspace / f"{c.id}.exit")}
    """).strip()
    return TestAction(
        criterion_id=c.id,
        description=f"STUB: {c.id} (missing params)",
        shell_snippet=snippet,
        artifact_paths=(f"{c.id}.exit", f"{c.id}.stdout", f"{c.id}.stderr"),
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
    # Both capture a command's stdout+stderr into separate artifact files;
    # their Verifier handlers read those streams (stream_separation checks
    # they stayed separated, json_lines_valid parses one channel line-by-line).
    "stream_separation": _emit_command_capture,
    "json_lines_valid": _emit_command_capture,
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
