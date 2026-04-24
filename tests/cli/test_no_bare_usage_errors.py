"""Blocking lint test: forbid new bare ``click.UsageError`` / ``click.ClickException``
/ ``raise SystemExit(<int>)`` / ``sys.exit(<int>)`` raises in ``src/codeprobe/cli/``.

PRD reference: Q15 + M-Mod "CI test posture".

How it works:

1. AST-walk every ``src/codeprobe/cli/**/*.py`` file (not regex).
2. Flag any ``ast.Raise`` whose ``exc`` is a call to one of:
      * ``click.UsageError(...)`` / bare ``UsageError(...)``
      * ``click.ClickException(...)`` / bare ``ClickException(...)``
      * ``SystemExit(<non-zero int literal>)``
   …and any top-level ``sys.exit(<non-zero int literal>)`` call expression.
3. Respect ``# lint-exempt: <reason>`` pragma on the same line or the
   immediately-preceding line.
4. Compare against :data:`INITIAL_WHITELIST` — a frozen snapshot of pre-existing
   violations. The test only fails on *new* violations. This lets us land the
   test BEFORE the error-migration unit.

INITIAL_WHITELIST policy: entries only come OFF as call sites migrate to the
new ``PrescriptiveError`` / ``DiagnosticError`` contract. Do NOT add entries.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Scan root — independent of cwd
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_CLI_ROOT = _REPO_ROOT / "src" / "codeprobe" / "cli"

# ---------------------------------------------------------------------------
# Pre-existing violation snapshot
#
# DO NOT add entries here — they only come off as call sites migrate to
# PrescriptiveError / DiagnosticError. Growing this list silently defeats the
# purpose of the lint gate.
# ---------------------------------------------------------------------------

INITIAL_WHITELIST: frozenset[tuple[str, int]] = frozenset(
    {
        ("src/codeprobe/cli/__init__.py", 1032),
        ("src/codeprobe/cli/_output_helpers.py", 109),
        ("src/codeprobe/cli/assess_cmd.py", 72),
        ("src/codeprobe/cli/assess_cmd.py", 75),
        ("src/codeprobe/cli/auth_cmd.py", 41),
        ("src/codeprobe/cli/experiment_cmd.py", 52),
        ("src/codeprobe/cli/experiment_cmd.py", 64),
        ("src/codeprobe/cli/experiment_cmd.py", 71),
        ("src/codeprobe/cli/experiment_cmd.py", 86),
        ("src/codeprobe/cli/experiment_cmd.py", 97),
        ("src/codeprobe/cli/experiment_cmd.py", 155),
        ("src/codeprobe/cli/experiment_cmd.py", 164),
        ("src/codeprobe/cli/experiment_cmd.py", 180),
        ("src/codeprobe/cli/experiment_cmd.py", 207),
        ("src/codeprobe/cli/experiment_cmd.py", 234),
        ("src/codeprobe/cli/experiment_cmd.py", 286),
        ("src/codeprobe/cli/experiment_cmd.py", 297),
        ("src/codeprobe/cli/experiment_cmd.py", 357),
        ("src/codeprobe/cli/experiment_cmd.py", 364),
        ("src/codeprobe/cli/init_cmd.py", 46),
        ("src/codeprobe/cli/probe_cmd.py", 100),
        ("src/codeprobe/cli/ratings_cmd.py", 74),
        ("src/codeprobe/cli/ratings_cmd.py", 118),
        ("src/codeprobe/cli/scaffold_cmd.py", 74),
        ("src/codeprobe/cli/scaffold_cmd.py", 100),
        ("src/codeprobe/cli/scaffold_cmd.py", 123),
        ("src/codeprobe/cli/trace_cmd.py", 36),
        ("src/codeprobe/cli/validate_cmd.py", 544),
        ("src/codeprobe/cli/validate_cmd.py", 558),
    }
)

_FORBIDDEN_EXC_NAMES: frozenset[str] = frozenset({"UsageError", "ClickException"})
_LINT_EXEMPT_MARKER = "# lint-exempt:"


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _is_nonzero_int_constant(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        # Exclude booleans — bool is a subclass of int but semantically distinct.
        and not isinstance(node.value, bool)
        and node.value != 0
    )


def _is_forbidden_raise_call(exc: ast.AST | None) -> bool:
    """Return True if ``exc`` is a forbidden call node inside a ``raise``."""
    if not isinstance(exc, ast.Call):
        return False
    func = exc.func

    # SystemExit(<non-zero int literal>)
    if isinstance(func, ast.Name) and func.id == "SystemExit":
        return bool(exc.args) and _is_nonzero_int_constant(exc.args[0])

    # click.UsageError(...) / click.ClickException(...)
    if isinstance(func, ast.Attribute) and func.attr in _FORBIDDEN_EXC_NAMES:
        return True

    # Bare UsageError(...) / ClickException(...) (imported directly)
    if isinstance(func, ast.Name) and func.id in _FORBIDDEN_EXC_NAMES:
        return True

    return False


def _is_sys_exit_expr(node: ast.AST) -> bool:
    """Match ``sys.exit(<non-zero int literal>)`` as a standalone call expression."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "exit"):
        return False
    if not (isinstance(func.value, ast.Name) and func.value.id == "sys"):
        return False
    return bool(node.args) and _is_nonzero_int_constant(node.args[0])


def _has_lint_exempt(source_lines: list[str], lineno: int) -> bool:
    """True if ``# lint-exempt:`` appears on this line or the previous line."""
    # lineno is 1-based
    for ln in (lineno, lineno - 1):
        if 1 <= ln <= len(source_lines):
            if _LINT_EXEMPT_MARKER in source_lines[ln - 1]:
                return True
    return False


def _scan_file(path: Path, rel_path: str) -> list[tuple[str, int]]:
    """Return a list of (rel_path, lineno) violations found in ``path``."""
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        # Unparseable files don't count as violations — surface as a separate
        # failure if this ever happens.
        return []

    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and _is_forbidden_raise_call(node.exc):
            if not _has_lint_exempt(lines, node.lineno):
                hits.append((rel_path, node.lineno))
        elif isinstance(node, ast.Expr) and _is_sys_exit_expr(node.value):
            if not _has_lint_exempt(lines, node.lineno):
                hits.append((rel_path, node.lineno))
    return hits


def _scan_cli_tree() -> list[tuple[str, int]]:
    """Walk every .py file under ``src/codeprobe/cli/`` and collect violations."""
    violations: list[tuple[str, int]] = []
    for py in sorted(_CLI_ROOT.rglob("*.py")):
        rel = py.relative_to(_REPO_ROOT).as_posix()
        violations.extend(_scan_file(py, rel))
    return violations


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cli_root_exists() -> None:
    """Sanity check — if this fails, the repo layout changed."""
    assert _CLI_ROOT.is_dir(), f"expected CLI dir at {_CLI_ROOT}"


def test_no_new_bare_usage_errors() -> None:
    """Fail on any forbidden raise not present in INITIAL_WHITELIST."""
    violations = set(_scan_cli_tree())
    new_violations = violations - INITIAL_WHITELIST

    if new_violations:
        formatted = "\n".join(
            f"  - {rel}:{ln}" for rel, ln in sorted(new_violations)
        )
        pytest.fail(
            "New bare-raise violations detected in src/codeprobe/cli/.\n"
            "Use PrescriptiveError or DiagnosticError from "
            "codeprobe.cli.errors instead of click.UsageError / "
            "click.ClickException / raise SystemExit(<int>) / sys.exit(<int>).\n"
            "If a raise is intentional (e.g. legitimate signal re-raise), "
            "add a `# lint-exempt: <reason>` comment on the same or preceding line.\n\n"
            f"New violations ({len(new_violations)}):\n{formatted}"
        )


def test_whitelist_does_not_contain_stale_entries() -> None:
    """INITIAL_WHITELIST entries must correspond to real, current violations.

    If a raise has been removed or migrated, its whitelist entry becomes stale.
    Stale entries should be deleted so the whitelist continues to shrink as
    migrations complete.
    """
    violations = set(_scan_cli_tree())
    stale = INITIAL_WHITELIST - violations

    if stale:
        formatted = "\n".join(f"  - {rel}:{ln}" for rel, ln in sorted(stale))
        pytest.fail(
            "INITIAL_WHITELIST contains entries that no longer match a real "
            "violation. Delete these entries — the whitelist must only shrink.\n\n"
            f"Stale entries ({len(stale)}):\n{formatted}"
        )


def test_lint_exempt_pragma_suppresses_violation(tmp_path: Path) -> None:
    """Synthetic check: the `# lint-exempt:` pragma actually suppresses a hit."""
    fake = tmp_path / "fake_cmd.py"
    fake.write_text(
        "import sys\n"
        "def go() -> None:\n"
        "    sys.exit(7)  # lint-exempt: intentional exit code forwarding\n"
    )
    hits = _scan_file(fake, fake.as_posix())
    assert hits == [], f"pragma should have suppressed hits, got {hits}"


def test_scanner_flags_forbidden_patterns(tmp_path: Path) -> None:
    """Synthetic check: each forbidden pattern is flagged when not exempt."""
    fake = tmp_path / "violations.py"
    fake.write_text(
        "import sys\n"
        "import click\n"
        "from click import UsageError\n"
        "\n"
        "def a() -> None:\n"
        "    raise click.UsageError('bad')\n"  # line 6
        "\n"
        "def b() -> None:\n"
        "    raise click.ClickException('bad')\n"  # line 9
        "\n"
        "def c() -> None:\n"
        "    raise SystemExit(1)\n"  # line 12
        "\n"
        "def d() -> None:\n"
        "    sys.exit(2)\n"  # line 15
        "\n"
        "def e() -> None:\n"
        "    raise UsageError('bad')\n"  # line 18
        "\n"
        "def f() -> None:\n"
        "    raise SystemExit(0)  # zero exit — allowed\n"  # line 21
        "\n"
        "def g() -> None:\n"
        "    raise SystemExit('string message — allowed')\n"  # line 24
    )
    hits = {ln for _, ln in _scan_file(fake, fake.as_posix())}
    assert hits == {6, 9, 12, 15, 18}, f"unexpected hits: {sorted(hits)}"
