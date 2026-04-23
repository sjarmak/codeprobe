#!/usr/bin/env python3
"""ZFC boundary lint rule (INV3).

Scans Python source files for assignments of hardcoded categorical
string literals (e.g. ``"low" / "medium" / "high" / "easy" / "hard"``)
to TaskMetadata-shaped attributes (``difficulty``, ``benefit``,
``quality_band``, anything starting with ``expected_``) without a
preceding model-invocation call in the same function scope.

The rule is STRUCTURAL:

    <model_invocation_call>  # required somewhere earlier in the function
    ...
    meta.difficulty = "hard"  # OK because model judged it

vs the violating shape:

    def make_task():
        meta.difficulty = "hard"  # violation: no model in scope

The banned-word set is a signal filter, not the rule. The real check is
"was a model invocation seen before this assignment in the enclosing
function body?"

Usage:
    python scripts/lint_zfc.py [--allowlist PATH] PATH [PATH ...]

Exit codes:
    0   no findings (or all findings suppressed by the allowlist)
    1   at least one unsuppressed finding — listed on stderr
    2   CLI / IO error

This is a standalone script — it imports only stdlib modules so it can
run before the project's dependencies are installed.
"""

from __future__ import annotations

import argparse
import ast
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Structural configuration (NOT heuristics — these are the bounded sets the
# rule applies to; tuning them does not weaken the structural check).
# ---------------------------------------------------------------------------

#: Function names that count as a model invocation marker.
MODEL_INVOCATION_NAMES: frozenset[str] = frozenset(
    {
        "invoke_model",
        "call_claude",
        "call_llm",
    }
)

#: Categorical string literals that act as a signal filter for banned
#: vocabulary. The structural rule fires regardless of the word list;
#: filtering on this set keeps reports focused on actual semantic tiers.
BANNED_VOCAB: frozenset[str] = frozenset(
    {
        "low",
        "medium",
        "high",
        "easy",
        "hard",
        "trivial",
    }
)

#: Fixed attribute names that indicate TaskMetadata semantics.
TASK_META_FIXED_ATTRS: frozenset[str] = frozenset(
    {
        "difficulty",
        "benefit",
        "quality_band",
    }
)


def _is_task_meta_attr(attr_name: str) -> bool:
    """Return True when an attribute name looks like TaskMetadata shape."""
    if attr_name in TASK_META_FIXED_ATTRS:
        return True
    return attr_name.startswith("expected_")


# ---------------------------------------------------------------------------
# Finding + allowlist data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """A single lint violation."""

    file: str
    line: int
    rule: str
    detail: str

    def format(self) -> str:
        """Machine-parseable ``file:line:rule detail`` line."""
        return f"{self.file}:{self.line}:{self.rule} {self.detail}"


@dataclass(frozen=True)
class AllowlistEntry:
    """A single allowlisted region inside the lint allowlist TOML."""

    file: str
    line_start: int
    line_end: int
    reason: str


def load_allowlist(path: Path) -> tuple[AllowlistEntry, ...]:
    """Load the lint allowlist TOML and return entries as a tuple.

    Returns an empty tuple if the allowlist file does not exist.
    Raises ``ValueError`` on malformed entries (validate-or-die).
    """
    if not path.exists():
        return ()

    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Allowlist {path} is not valid TOML: {exc}") from exc

    raw_entries = data.get("entry", [])
    if not isinstance(raw_entries, list):
        raise ValueError(
            f"Allowlist {path}: expected [[entry]] array, got "
            f"{type(raw_entries).__name__}"
        )

    entries: list[AllowlistEntry] = []
    for idx, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            raise ValueError(f"Allowlist {path}: entry #{idx} is not a table")
        file_val = raw.get("file")
        if not isinstance(file_val, str) or not file_val:
            raise ValueError(
                f"Allowlist {path}: entry #{idx} missing string 'file'"
            )
        line = raw.get("line")
        line_start = raw.get("line_start", line)
        line_end = raw.get("line_end", line_start)
        if not isinstance(line_start, int) or not isinstance(line_end, int):
            raise ValueError(
                f"Allowlist {path}: entry #{idx} 'line'/'line_start'/'line_end' "
                f"must be integers"
            )
        reason = raw.get("reason", "")
        if not isinstance(reason, str):
            raise ValueError(
                f"Allowlist {path}: entry #{idx} 'reason' must be a string"
            )
        entries.append(
            AllowlistEntry(
                file=file_val,
                line_start=line_start,
                line_end=line_end,
                reason=reason,
            )
        )
    return tuple(entries)


def is_suppressed(
    finding: Finding, allowlist: Iterable[AllowlistEntry]
) -> bool:
    """Return True when ``finding`` falls inside an allowlist region.

    Matching is done on normalized POSIX-style paths — the finding path
    must equal the allowlist file path exactly, or end with it (so the
    allowlist entry is the shorter, anchored suffix — e.g.
    ``src/codeprobe/mining/extractor.py`` matches a finding at
    ``/abs/path/to/src/codeprobe/mining/extractor.py``).

    The inverse direction — allowlist entry being a superset of the
    finding path — is intentionally NOT accepted. That would let a
    longer allowlist entry silently suppress findings from an unrelated
    shorter-path source file that happens to share a basename/suffix
    (see v0.6.0-batch-a review).
    """
    finding_posix = finding.file.replace("\\", "/")
    for entry in allowlist:
        entry_posix = entry.file.replace("\\", "/")
        if not (
            finding_posix == entry_posix
            or finding_posix.endswith("/" + entry_posix)
        ):
            continue
        if entry.line_start <= finding.line <= entry.line_end:
            return True
    return False


# ---------------------------------------------------------------------------
# AST analysis
# ---------------------------------------------------------------------------


def _callable_name(func_node: ast.expr) -> str | None:
    """Return the ``Name.id`` or ``Attribute.attr`` tail of a Call target."""
    if isinstance(func_node, ast.Name):
        return func_node.id
    if isinstance(func_node, ast.Attribute):
        return func_node.attr
    return None


def _is_model_invocation(call: ast.Call) -> bool:
    name = _callable_name(call.func)
    return name is not None and name in MODEL_INVOCATION_NAMES


def _is_task_metadata_call(call: ast.Call) -> bool:
    """Check if Call targets a symbol named TaskMetadata."""
    return _callable_name(call.func) == "TaskMetadata"


def _banned_string(node: ast.AST) -> str | None:
    """Return the literal if ``node`` is a banned-vocab string constant."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if node.value in BANNED_VOCAB:
            return node.value
    return None


def _iter_function_scopes(
    tree: ast.AST,
) -> Iterable[ast.FunctionDef | ast.AsyncFunctionDef | ast.Module]:
    """Yield every module + function/async-function scope in ``tree``."""
    if isinstance(tree, ast.Module):
        yield tree
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _walk_scope_statements(
    scope: ast.FunctionDef | ast.AsyncFunctionDef | ast.Module,
) -> Iterable[ast.AST]:
    """Walk a scope's body, yielding descendants but NOT crossing into
    nested function definitions (those are reported as their own scopes).

    The walk preserves source order via AST walking with pruning.
    """
    # Use an explicit stack so we control traversal.
    # Each stack entry is a node whose children we still need to visit.
    # We iterate in source order by pushing children in reverse so the
    # first child is popped first.
    stack: list[ast.AST] = list(reversed(list(ast.iter_child_nodes(scope))))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Don't descend into nested functions — they're separate scopes.
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def analyze_source(source: str, filename: str) -> list[Finding]:
    """Run the ZFC boundary rule over ``source`` and return findings."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        # A syntax error in a scanned file is not our job to fix — surface
        # it as a parse error finding so the caller knows.
        return [
            Finding(
                file=filename,
                line=exc.lineno or 0,
                rule="zfc-parse-error",
                detail=str(exc.msg),
            )
        ]

    findings: list[Finding] = []
    for scope in _iter_function_scopes(tree):
        findings.extend(_analyze_scope(scope, filename))
    return findings


def _analyze_scope(
    scope: ast.FunctionDef | ast.AsyncFunctionDef | ast.Module,
    filename: str,
) -> list[Finding]:
    """Scan a single function or module scope in source order."""
    model_seen = False
    findings: list[Finding] = []

    for node in _walk_scope_statements(scope):
        # 1. Model invocation detection — update the flag first so that
        #    an assignment on the SAME line as the model call (rare) is
        #    still considered "after" it.
        if isinstance(node, ast.Call) and _is_model_invocation(node):
            model_seen = True
            continue

        # 2. TaskMetadata(...) constructor with banned kwarg values.
        if isinstance(node, ast.Call) and _is_task_metadata_call(node):
            for kw in node.keywords:
                if kw.arg is None:
                    # **kwargs expansion — can't inspect statically.
                    continue
                if not _is_task_meta_attr(kw.arg):
                    continue
                banned = _banned_string(kw.value)
                if banned is None:
                    continue
                if model_seen:
                    continue
                findings.append(
                    Finding(
                        file=filename,
                        line=kw.value.lineno
                        if hasattr(kw.value, "lineno")
                        else node.lineno,
                        rule="zfc-hardcoded-task-metadata",
                        detail=f"TaskMetadata({kw.arg}={banned!r}) without model invocation",
                    )
                )
            # Note: we do NOT `continue` — fall through so the Call node's
            # child nodes are still walked for nested assignments, but
            # the TaskMetadata call itself is not a model invocation.
            continue

        # 3. Direct attribute assignment: meta.difficulty = "hard"
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value

        if value is None:
            continue

        for target in targets:
            if not isinstance(target, ast.Attribute):
                continue
            if not _is_task_meta_attr(target.attr):
                continue
            banned = _banned_string(value)
            if banned is None:
                continue
            if model_seen:
                continue
            findings.append(
                Finding(
                    file=filename,
                    line=target.lineno,
                    rule="zfc-hardcoded-task-metadata",
                    detail=f".{target.attr} = {banned!r} without model invocation",
                )
            )

    return findings


# ---------------------------------------------------------------------------
# File walking + CLI
# ---------------------------------------------------------------------------


def iter_python_files(paths: Iterable[Path]) -> Iterable[Path]:
    """Yield every ``.py`` file under the given paths (files or dirs)."""
    for path in paths:
        if path.is_file() and path.suffix == ".py":
            yield path
            continue
        if path.is_dir():
            for sub in sorted(path.rglob("*.py")):
                yield sub


def analyze_files(
    files: Iterable[Path],
    allowlist: Iterable[AllowlistEntry],
) -> list[Finding]:
    """Analyze a list of files and return the non-suppressed findings."""
    reported: list[Finding] = []
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            reported.append(
                Finding(
                    file=str(path),
                    line=0,
                    rule="zfc-io-error",
                    detail=str(exc),
                )
            )
            continue
        for finding in analyze_source(source, str(path)):
            if is_suppressed(finding, allowlist):
                continue
            reported.append(finding)
    return reported


def build_parser() -> argparse.ArgumentParser:
    """Return the argparse parser — factored out for ``--help`` testing."""
    parser = argparse.ArgumentParser(
        prog="lint_zfc",
        description=(
            "ZFC boundary lint: flag hardcoded categorical string "
            "assignments to TaskMetadata-shaped attributes without a "
            "preceding model invocation call."
        ),
    )
    default_allowlist = Path(__file__).with_name("lint_zfc.allowlist.toml")
    parser.add_argument(
        "paths",
        nargs="+",
        help="Files or directories to scan (directories are walked for *.py).",
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=default_allowlist,
        help=(
            "Path to allowlist TOML (default: lint_zfc.allowlist.toml "
            "next to this script). Missing file is treated as empty."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    paths = [Path(p) for p in args.paths]
    for p in paths:
        if not p.exists():
            print(f"lint_zfc: path not found: {p}", file=sys.stderr)
            return 2

    try:
        allowlist = load_allowlist(args.allowlist)
    except ValueError as exc:
        print(f"lint_zfc: {exc}", file=sys.stderr)
        return 2

    findings = analyze_files(iter_python_files(paths), allowlist)

    if not findings:
        return 0

    for finding in findings:
        print(finding.format(), file=sys.stderr)
    print(
        f"lint_zfc: {len(findings)} finding(s) — see above",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
