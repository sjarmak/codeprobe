"""Blocking drift test: every ``code=`` string literal passed to
``PrescriptiveError`` / ``DiagnosticError`` must be present in the public
error-code catalog at ``src/codeprobe/cli/error_codes.json``.

PRD reference: Q15 + M-Mod "CI test posture".

How it works:

1. AST-walk ``src/codeprobe/cli/**/*.py``.
2. For every ``ast.Call`` whose function resolves to ``PrescriptiveError`` or
   ``DiagnosticError`` (bare name OR attribute access, e.g. ``errors.PrescriptiveError``),
   extract the ``code`` string literal. Accept both keyword (``code="X"``) and
   first-positional (``PrescriptiveError("X", ...)``) forms.
3. Load the catalog JSON once and build a set of known codes.
4. Fail with a diff if any code used in source is missing from the catalog.

Notes:

* The catalog is consulted at the top-level key ``codes`` (list of objects with
  a ``code`` field) when present, or any object/list of strings otherwise.
  This is deliberately permissive so the exact catalog schema can be pinned
  later without breaking this test.
* On the current codebase, the error-migration unit hasn't landed yet, so the
  set of discovered ``code=`` literals may be empty and the test passes
  trivially. The synthetic ``tmp_path`` fixture test validates the detection
  logic itself regardless.
"""

from __future__ import annotations

import ast
import json
import textwrap
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_CLI_ROOT = _REPO_ROOT / "src" / "codeprobe" / "cli"
_CATALOG_PATH = _CLI_ROOT / "error_codes.json"

_ERROR_CLASS_NAMES: frozenset[str] = frozenset(
    {"PrescriptiveError", "DiagnosticError"}
)


# ---------------------------------------------------------------------------
# Catalog loader — permissive about schema
# ---------------------------------------------------------------------------


def _load_catalog_codes(catalog_path: Path) -> set[str]:
    """Load the error-code catalog and return the set of known codes.

    Accepts several shapes so the catalog schema can evolve without breaking
    the lint test:

    * ``{"codes": [{"code": "FOO", ...}, ...]}``
    * ``{"codes": ["FOO", "BAR", ...]}``
    * ``{"FOO": {...}, "BAR": {...}}`` — keys treated as codes
    * ``["FOO", "BAR", ...]``

    Returns an empty set if the catalog does not yet exist (Layer 0 bootstrap).
    """
    if not catalog_path.exists():
        return set()

    raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    codes: set[str] = set()

    if isinstance(raw, dict) and "codes" in raw:
        entries = raw["codes"]
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, str):
                    codes.add(entry)
                elif isinstance(entry, dict) and isinstance(entry.get("code"), str):
                    codes.add(entry["code"])
        return codes

    if isinstance(raw, dict):
        return {k for k in raw.keys() if isinstance(k, str)}

    if isinstance(raw, list):
        return {item for item in raw if isinstance(item, str)}

    return codes


# ---------------------------------------------------------------------------
# AST extraction
# ---------------------------------------------------------------------------


def _call_target_name(call: ast.Call) -> str | None:
    """Return the simple class name a ``Call`` resolves to, or None."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _extract_code_literal(call: ast.Call) -> str | None:
    """Return the string literal used for the ``code`` argument, else None.

    Accepts ``PrescriptiveError(code="X", ...)`` and
    ``PrescriptiveError("X", ...)`` (positional first-arg fallback).
    """
    for kw in call.keywords:
        if kw.arg == "code":
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value
            # Non-literal `code=` → skip (we only police static string literals)
            return None

    # Fall back to first positional arg as code if no keyword form found
    if call.args:
        first = call.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def _scan_source_for_codes(path: Path) -> list[tuple[str, int, str]]:
    """Return (rel_path, lineno, code) for every detected call site.

    ``rel_path`` is posix and relative to :data:`_REPO_ROOT` when ``path`` is
    inside the repo; otherwise it's ``path.as_posix()`` (tmp_path case).
    """
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    try:
        rel = path.resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        rel = path.as_posix()

    hits: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_target_name(node)
        if name not in _ERROR_CLASS_NAMES:
            continue
        code = _extract_code_literal(node)
        if code is None:
            continue
        hits.append((rel, node.lineno, code))
    return hits


def _scan_cli_tree_for_codes() -> list[tuple[str, int, str]]:
    results: list[tuple[str, int, str]] = []
    for py in sorted(_CLI_ROOT.rglob("*.py")):
        results.extend(_scan_source_for_codes(py))
    return results


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cli_root_exists() -> None:
    """Sanity check — if this fails, the repo layout changed."""
    assert _CLI_ROOT.is_dir(), f"expected CLI dir at {_CLI_ROOT}"


def test_error_codes_catalog_is_valid_json_when_present() -> None:
    """If the catalog file exists, it must be valid JSON and parseable."""
    if not _CATALOG_PATH.exists():
        pytest.skip(
            "error_codes.json not yet created — Layer 0 error-migration "
            "hasn't landed. The drift test still runs against the (empty) catalog."
        )
    # Will raise json.JSONDecodeError if invalid — surfaces clearly in pytest.
    json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))


def test_no_drift_between_source_codes_and_catalog() -> None:
    """Every ``code=`` literal in source must exist in error_codes.json."""
    known_codes = _load_catalog_codes(_CATALOG_PATH)
    hits = _scan_cli_tree_for_codes()

    missing: list[tuple[str, int, str]] = [
        (rel, ln, code) for rel, ln, code in hits if code not in known_codes
    ]

    if missing:
        formatted = "\n".join(
            f"  - {rel}:{ln}  code={code!r}" for rel, ln, code in sorted(missing)
        )
        all_known = ", ".join(sorted(known_codes)) if known_codes else "<empty catalog>"
        pytest.fail(
            "Error-code drift detected: the following codes are used in "
            "src/codeprobe/cli/ but are NOT present in "
            f"{_CATALOG_PATH.relative_to(_REPO_ROOT)}.\n"
            "Either add them to the catalog with a user-facing description, "
            "or correct the spelling at the call site.\n\n"
            f"Missing codes ({len(missing)}):\n{formatted}\n\n"
            f"Known codes in catalog: {all_known}"
        )


def test_drift_detector_catches_synthetic_bogus_code(tmp_path: Path) -> None:
    """Negative test — proves the scan itself surfaces unknown codes.

    Writes a synthetic source file containing a ``PrescriptiveError`` with a
    code we know is NOT in the catalog, then runs the same scan + drift check
    we use in production and asserts the bogus code is flagged.
    """
    fake = tmp_path / "bogus_code_fixture.py"
    fake.write_text(
        textwrap.dedent(
            """
            from codeprobe.cli.errors import PrescriptiveError, DiagnosticError


            def raise_bogus() -> None:
                raise PrescriptiveError(
                    code="TOTALLY_BOGUS_CODE",
                    next_try_flag="--x",
                    next_try_value="y",
                )


            def raise_positional_bogus() -> None:
                raise DiagnosticError(
                    "ANOTHER_BOGUS_CODE",
                    detail="positional first-arg form",
                )
            """
        ).strip()
        + "\n"
    )

    hits = _scan_source_for_codes(fake)
    codes = {code for _, _, code in hits}

    assert "TOTALLY_BOGUS_CODE" in codes, (
        f"scanner failed to extract keyword `code=` literal. Hits: {hits}"
    )
    assert "ANOTHER_BOGUS_CODE" in codes, (
        f"scanner failed to extract positional first-arg code literal. Hits: {hits}"
    )

    # Now run the same drift check logic against a deliberately empty catalog.
    known_codes: set[str] = set()
    missing = [(rel, ln, code) for rel, ln, code in hits if code not in known_codes]
    assert {code for _, _, code in missing} == {
        "TOTALLY_BOGUS_CODE",
        "ANOTHER_BOGUS_CODE",
    }, f"drift detector did not flag bogus codes: {missing}"


def test_scan_ignores_non_error_calls(tmp_path: Path) -> None:
    """Guard against false positives — other classes must not be picked up."""
    fake = tmp_path / "unrelated.py"
    fake.write_text(
        textwrap.dedent(
            """
            class SomethingElse:
                pass


            def go() -> None:
                SomethingElse(code="NOT_AN_ERROR_CODE")
                some_func(code="ALSO_NOT")
            """
        ).strip()
        + "\n"
    )
    hits = _scan_source_for_codes(fake)
    assert hits == [], f"scan should ignore unrelated calls, got {hits}"
