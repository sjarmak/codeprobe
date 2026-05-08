"""Oracle coherence checks.

Verifies that the *files and symbols the instruction text claims to touch*
actually exist, are written in an allowed language, and live inside the
declared path scope. This is the most expensive of the three checks because
it touches the filesystem, but it has no agent calls and is fully
deterministic.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

from codeprobe.qa.benchmark_qa_core._symbols import symbol_in_file
from codeprobe.qa.benchmark_qa_core.types import (
    DEFAULT_LANGUAGE_EXTENSIONS,
    Finding,
    OracleConstraints,
    Severity,
)


def check_oracle_coherence(
    instruction_text: str,
    oracle_files: list[str],
    oracle_symbols: list[tuple[str, str]],
    repo_root: Path,
    constraints: OracleConstraints,
) -> list[Finding]:
    """Validate that oracle files, symbols, languages, and paths are coherent.

    All inputs are already-parsed by the caller (the rig adapter). This
    function only does mechanical lookups — no schema parsing, no agent
    judgment.

    Checks performed:

    * **A1** — every oracle file path exists under ``repo_root``.
    * **B1** — every ``(file, symbol)`` pair resolves: the symbol is found in
      the file via ast-grep (or regex fallback). Severity is ``error`` when
      ``constraints.require_symbols_resolve`` is true, ``warning`` otherwise.
    * **B2** — symbol references a file that doesn't exist (downgraded sibling
      of A1, scoped to the symbol pair so callers can pin it independently).
    * **C1** — oracle file's extension doesn't map to any of
      ``constraints.expected_languages``.
    * **D1** — oracle file matches none of ``constraints.path_include`` (only
      checked when ``path_include`` is non-empty).
    * **D2** — oracle file matches one of ``constraints.path_exclude``.

    Args:
        instruction_text: Raw task instruction. Currently unused for filtering
            but kept in the signature so future checks (e.g. cross-referencing
            file mentions inside the instruction) don't break the contract.
        oracle_files: Repo-relative file paths that the task's oracle expects
            the agent to touch.
        oracle_symbols: ``(file, symbol)`` pairs the oracle expects to exist.
            ``file`` is repo-relative.
        repo_root: Absolute path to the repo root. Used to resolve the
            relative paths above.
        constraints: Knobs scoping language/path/symbol enforcement. See
            :class:`OracleConstraints`.

    Returns:
        Flat list of findings, in roughly the order the checks ran. The list
        is empty when the oracle is fully coherent.
    """
    del instruction_text  # reserved for future cross-reference checks
    findings: list[Finding] = []
    ext_table = constraints.language_extensions or DEFAULT_LANGUAGE_EXTENSIONS

    # A1, C1, D1, D2 — per oracle file
    for rel in oracle_files:
        abs_path = (repo_root / rel).resolve()
        if not abs_path.exists():
            findings.append(
                Finding(
                    severity="error",
                    code="A1",
                    message=f"Oracle file does not exist: {rel}",
                    location=rel,
                    suggested_fix=(
                        "Remove the entry from oracle_files or add the file "
                        "to the repo snapshot."
                    ),
                )
            )
            continue

        if constraints.expected_languages:
            ext = abs_path.suffix.lower()
            language = ext_table.get(ext)
            if language is None or language not in constraints.expected_languages:
                expected = ", ".join(sorted(constraints.expected_languages))
                findings.append(
                    Finding(
                        severity="error",
                        code="C1",
                        message=(
                            f"Oracle file extension {ext or '(none)'!r} does "
                            f"not match expected languages: {expected}"
                        ),
                        location=rel,
                        suggested_fix=(
                            "Adjust expected_languages, or remove the file "
                            "from the oracle if it's not part of the task."
                        ),
                    )
                )

        if constraints.path_include and not _matches_any(rel, constraints.path_include):
            patterns = ", ".join(constraints.path_include)
            findings.append(
                Finding(
                    severity="error",
                    code="D1",
                    message=(
                        f"Oracle path is outside the include scope. "
                        f"Patterns: {patterns}"
                    ),
                    location=rel,
                    suggested_fix=(
                        "Move the file under an included path, widen "
                        "path_include, or remove the entry."
                    ),
                )
            )

        if constraints.path_exclude and _matches_any(rel, constraints.path_exclude):
            patterns = ", ".join(constraints.path_exclude)
            findings.append(
                Finding(
                    severity="error",
                    code="D2",
                    message=(
                        f"Oracle path is in the exclude scope. "
                        f"Patterns: {patterns}"
                    ),
                    location=rel,
                    suggested_fix=(
                        "Remove the file from oracle_files or relax "
                        "path_exclude."
                    ),
                )
            )

    # B1, B2 — per (file, symbol) pair
    severity: Severity = "error" if constraints.require_symbols_resolve else "warning"
    for rel, symbol in oracle_symbols:
        abs_path = (repo_root / rel).resolve()
        if not abs_path.exists():
            findings.append(
                Finding(
                    severity=severity,
                    code="B2",
                    message=(
                        f"Oracle symbol references a missing file: "
                        f"{symbol!r} in {rel}"
                    ),
                    location=f"{rel}::{symbol}",
                    suggested_fix=(
                        "Restore the file or remove the symbol from "
                        "oracle_symbols."
                    ),
                )
            )
            continue
        if not symbol_in_file(abs_path, symbol):
            findings.append(
                Finding(
                    severity=severity,
                    code="B1",
                    message=(
                        f"Oracle symbol not found in file: {symbol!r} not in "
                        f"{rel}"
                    ),
                    location=f"{rel}::{symbol}",
                    suggested_fix=(
                        "Verify the symbol name and path, or update the "
                        "oracle to match the current code."
                    ),
                )
            )

    return findings


def _matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    """Return ``True`` when ``path`` matches at least one fnmatch pattern."""
    return any(fnmatch.fnmatch(path, pat) for pat in patterns)
