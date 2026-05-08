"""Aux-file leakage check.

Detects oracle tokens (e.g. expected function names, expected literal output)
that appear inside auxiliary context files the agent is allowed to read. A
hit means the task is leaking the answer into the prompt and the score from
that task is suspect.
"""

from __future__ import annotations

import re
from pathlib import Path

from codeprobe.qa.benchmark_qa_core.types import Finding


def check_aux_file_leakage(
    oracle_tokens: list[str],
    aux_files: list[Path],
) -> list[Finding]:
    """Flag oracle tokens that appear verbatim in aux files.

    The check is intentionally a literal word-boundary match. It does not try
    to be clever about programming-language structure; the goal is to catch
    the obvious failure mode where a task author drops the expected answer
    into a README or a test fixture that the agent will see.

    Findings:

    * **F1** — token is short enough that hits are likely false positives
      (length < 3). Emitted at ``info`` severity, no location, so callers can
      decide whether to surface it.
    * **F2** — token appears in an aux file. Emitted once per (token, file)
      pair at ``error`` severity.
    * **F3** — aux file path does not exist. Emitted at ``warning`` severity
      so the rig can prune its context list without failing the task.

    Tokens are matched as whole words via ``\\b<token>\\b``. Special regex
    characters in tokens are escaped, so callers can pass arbitrary strings.

    Args:
        oracle_tokens: Strings the agent should not see (e.g. expected return
            values, oracle symbol names, golden output snippets).
        aux_files: Files the agent has read access to during the task. The
            check reads each file as UTF-8 with replacement on decode errors.

    Returns:
        Flat list of findings, ordered token-major, file-minor.
    """
    findings: list[Finding] = []

    short_tokens = [tok for tok in oracle_tokens if len(tok) < 3]
    for tok in short_tokens:
        findings.append(
            Finding(
                severity="info",
                code="F1",
                message=(
                    f"Oracle token {tok!r} is shorter than 3 characters; "
                    "leakage matches are likely false positives."
                ),
                location=None,
                suggested_fix=(
                    "Use a more specific token, or filter short tokens "
                    "before calling check_aux_file_leakage."
                ),
            )
        )

    file_cache: dict[Path, str | None] = {}
    for tok in oracle_tokens:
        if len(tok) < 3:
            continue
        pattern = re.compile(rf"\b{re.escape(tok)}\b")
        for aux in aux_files:
            text = _read_or_cache(aux, file_cache, findings)
            if text is None:
                continue
            if pattern.search(text):
                findings.append(
                    Finding(
                        severity="error",
                        code="F2",
                        message=(
                            f"Oracle token {tok!r} appears in aux file: "
                            f"{aux}"
                        ),
                        location=str(aux),
                        suggested_fix=(
                            "Redact the token from the aux file or remove "
                            "the file from the agent's context."
                        ),
                    )
                )

    return findings


def _read_or_cache(
    aux: Path,
    cache: dict[Path, str | None],
    findings: list[Finding],
) -> str | None:
    """Read ``aux`` once, caching the result; emit F3 the first time it misses."""
    if aux in cache:
        return cache[aux]
    if not aux.exists() or not aux.is_file():
        cache[aux] = None
        findings.append(
            Finding(
                severity="warning",
                code="F3",
                message=f"Aux file does not exist or is not a regular file: {aux}",
                location=str(aux),
                suggested_fix="Remove the path from aux_files or restore the file.",
            )
        )
        return None
    try:
        text = aux.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        cache[aux] = None
        findings.append(
            Finding(
                severity="warning",
                code="F3",
                message=f"Failed to read aux file {aux}: {exc}",
                location=str(aux),
                suggested_fix="Check file permissions or remove from aux_files.",
            )
        )
        return None
    cache[aux] = text
    return text
