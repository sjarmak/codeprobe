#!/usr/bin/env python3
"""Bead-close evidence reachability check.

For one or more bead IDs, verify that:

  1. Every ``git:<sha>`` entry in ``metadata.evidence.artifact_path``
     is an ancestor of the target branch (default ``main``).
     A bead can not be "shipped" while its evidence commit lives only
     on a feature branch — the close-gate-reaper enforces evidence
     fields but has no view into git, so it cannot catch this.

  2. ``metadata.gate_bypass`` does not contain future-tense modal
     verbs ("will", "pending", "WIP", "in-progress", "TBD", etc.).
     Legitimate bypasses are exception cases ("duplicate-of",
     "superseded-by", "abandoned") — future-tense language signals
     deferred work being smuggled through as if it were complete.

  3. Any commit SHA *cited in prose* (``close_reason`` or ``notes``,
     via ``git:<sha>`` or ``@<sha>`` citation forms) is independently
     verified reachable too. ``codeprobe-3cs``/``codeprobe-1gg`` were
     closed 2026-06-21 with ``close_reason`` claiming "Merged to main
     @94d357c ... Published/reachable" — a claim only checked in the
     narrative, never in a field this script reads. ``94d357c`` was
     never an ancestor of ``main``. Free text is not evidence; this
     rule catches a bogus claim even when it never made it into
     ``metadata.evidence.artifact_path``.

  4. When ``metadata.evidence.artifact_path`` is set but contains no
     ``git:<sha>`` entry (a docs/test-only path), the bead must
     explicitly declare ``metadata.evidence.doc_only=true``. Without
     it, a doc path silently satisfied this checker with zero
     verification of whatever code change the bead actually claimed —
     the previous behavior treated that as automatically "acceptable."
     Genuinely doc-only beads (research, writeups) opt in explicitly;
     everything else must cite a real commit.

This script is a STRUCTURAL check — it does not judge whether the
work was actually done. It only catches the failure modes the
premortem identified for the evjr.* pattern (commits on feature
branches closed as shipped), the predicted "gate_bypass-as-
release-valve" pattern, and the prose-citation / doc-only-evidence
gaps found during the 2026-07-13 branch cleanup pass (3cs/1gg cited
an unreachable SHA only in prose; u7l/b9c disclosed "NOT merged" in
notes yet still closed on doc-only evidence with no mechanism forcing
the actual integration).

Usage:
    python scripts/check_bead_reachability.py <bead-id> [<bead-id> ...]
    python scripts/check_bead_reachability.py --epic <epic-id>
    python scripts/check_bead_reachability.py <bead-id> --json
    python scripts/check_bead_reachability.py <bead-id> --branch main

Exit codes:
    0   all checks pass (or bypass is legitimate)
    1   at least one violation (listed on stderr / in JSON output)
    2   CLI / IO error (bd not found, bead not found, git not in repo)

The script imports only stdlib so it can run in any codeprobe
checkout regardless of whether the project's dev dependencies are
installed — same constraint as ``scripts/lint_zfc.py``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Structural configuration (bounded sets — tuning them does not weaken the
# structural check).
# ---------------------------------------------------------------------------

#: Future-tense / deferred-work markers banned in ``gate_bypass`` strings.
#: Whole-word, case-insensitive. The premortem (R5, Team-Process lens)
#: identified this as the exact loophole that produced the evjr.* 13-cycle
#: reopen pattern.
BANNED_BYPASS_MARKERS: tuple[str, ...] = (
    r"\bwill\b",
    r"\bgoing\s+to\b",
    r"\bgonna\b",
    r"\bpending\b",
    r"\bwip\b",
    r"\bin[-\s]progress\b",
    r"\btbd\b",
    r"\btodo\b",
    r"\blater\b",
    r"\bsoon\b",
    r"\bnext\s+sprint\b",
    r"\bnext\s+quarter\b",
)

#: Bypass prefixes that the reaper itself documents as legitimate exceptions
#: (see CLAUDE.md "Bypass for legitimate exception cases"). These short-
#: circuit the future-tense check.
LEGITIMATE_BYPASS_PREFIXES: tuple[str, ...] = (
    "duplicate-of",
    "superseded-by",
    "abandoned",
    "obsolete",
    "won't-fix",
)

#: Compiled at import time so we pay the regex cost once per process.
_BANNED_BYPASS_RE = re.compile("|".join(BANNED_BYPASS_MARKERS), re.IGNORECASE)

#: Pattern that extracts ``git:<sha>`` entries from ``evidence.artifact_path``.
#: The field is comma-separated per CLAUDE.md; we match each git:sha entry
#: individually and tolerate ``-`` / surrounding whitespace.
_GIT_SHA_RE = re.compile(r"git:([0-9a-f]{7,40})", re.IGNORECASE)

#: The other SHA-citation style observed in the wild (e.g. close_reason text
#: "Merged to codeprobe main @94d357c ..."). Deliberately narrow — only the
#: two forms actually seen in this bead store are matched, to avoid
#: false-positiving on ordinary hex-looking words in prose.
_AT_SHA_RE = re.compile(r"@([0-9a-f]{7,40})\b", re.IGNORECASE)

#: Truthy string forms accepted for boolean-ish metadata values (bd metadata
#: values are always strings).
_TRUTHY_STRINGS = frozenset({"true", "yes", "1"})


@dataclass(frozen=True)
class Violation:
    """A single failed check on a single bead."""

    bead_id: str
    rule: str  # unreachable_sha | unreachable_prose_sha | banned_bypass |
    # missing_evidence | doc_only_evidence
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"bead_id": self.bead_id, "rule": self.rule, "detail": self.detail}


@dataclass
class BeadCheck:
    """Aggregated check results for one bead."""

    bead_id: str
    violations: list[Violation] = field(default_factory=list)
    shas_checked: list[str] = field(default_factory=list)
    bypass_legitimate: bool = False


# ---------------------------------------------------------------------------
# bd / git plumbing
# ---------------------------------------------------------------------------


def _run(cmd: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    """Run a subprocess capturing stdout/stderr as text.

    ``check=False`` returns the CompletedProcess even on non-zero exit;
    callers branch on ``returncode`` because we treat "not-an-ancestor"
    as data, not as an error.
    """
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=check,
    )


def fetch_bead(bead_id: str) -> dict | None:
    """Return the bd JSON for ``bead_id``, or ``None`` if not found."""
    proc = _run(["bd", "show", bead_id, "--long", "--json"])
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    # ``bd show`` returns a list even for a single ID.
    if isinstance(data, list):
        return data[0] if data else None
    return data


def fetch_epic_children(epic_id: str) -> list[str]:
    """Return the IDs of every child bead under ``epic_id``."""
    proc = _run(["bd", "show", epic_id, "--children", "--json"])
    if proc.returncode != 0:
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [entry["id"] for entry in data if isinstance(entry, dict) and "id" in entry]


def is_ancestor(sha: str, branch: str, repo_root: Path) -> bool:
    """True iff ``sha`` is an ancestor of ``branch`` in ``repo_root``."""
    proc = _run(
        ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", sha, branch]
    )
    # exit 0 = is ancestor, exit 1 = is not, exit other = error (e.g. unknown sha)
    return proc.returncode == 0


def _is_truthy(value: object) -> bool:
    """True iff a bd metadata value (always a string) reads as boolean-true."""
    return str(value).strip().lower() in _TRUTHY_STRINGS


def extract_prose_shas(text: str) -> list[str]:
    """Return SHA-shaped tokens cited via ``git:<sha>`` or ``@<sha>`` in *text*.

    Order-preserving, de-duplicated. Used against free-text fields
    (``close_reason``, ``notes``) where a closer can claim a merge that
    nothing else checks — the exact gap that let ``codeprobe-3cs``/
    ``codeprobe-1gg`` cite an unreachable SHA and still close clean.
    """
    seen: list[str] = []
    for pattern in (_GIT_SHA_RE, _AT_SHA_RE):
        for sha in pattern.findall(text):
            if sha not in seen:
                seen.append(sha)
    return seen


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_bead(bead_id: str, branch: str, repo_root: Path) -> BeadCheck:
    """Run all checks against one bead and aggregate violations."""
    result = BeadCheck(bead_id=bead_id)
    bead = fetch_bead(bead_id)
    if bead is None:
        result.violations.append(
            Violation(
                bead_id=bead_id,
                rule="missing_evidence",
                detail=f"bd show {bead_id} returned no record",
            )
        )
        return result

    metadata = bead.get("metadata") or {}

    # 1. Legitimate-bypass short-circuit. If the bead is closed under one of
    #    the documented exception reasons, skip the reachability + banned-
    #    bypass checks entirely. We still record that we checked.
    bypass = (metadata.get("gate_bypass") or "").strip()
    if bypass:
        lower = bypass.lower()
        if any(lower.startswith(prefix) for prefix in LEGITIMATE_BYPASS_PREFIXES):
            result.bypass_legitimate = True
            return result
        # 2. Bypass exists but is not in the legitimate prefix set. Check
        #    for banned future-tense markers.
        match = _BANNED_BYPASS_RE.search(bypass)
        if match:
            result.violations.append(
                Violation(
                    bead_id=bead_id,
                    rule="banned_bypass",
                    detail=(
                        f'gate_bypass="{bypass}" contains banned future-tense '
                        f'marker "{match.group(0)}"; legitimate bypass reasons '
                        f"start with one of: {', '.join(LEGITIMATE_BYPASS_PREFIXES)}"
                    ),
                )
            )
            # Even with a banned bypass, still check reachability — we want
            # the full picture, not a short-circuit on the first failure.

    # 3. Reachability check on every ``git:<sha>`` in evidence.artifact_path.
    artifact_path = metadata.get("evidence.artifact_path") or ""
    if not artifact_path and not bypass:
        # A closed bead with neither evidence nor bypass is the exact pattern
        # the reaper catches. Surface it.
        if bead.get("status") == "closed":
            result.violations.append(
                Violation(
                    bead_id=bead_id,
                    rule="missing_evidence",
                    detail=(
                        "closed bead has no metadata.evidence.artifact_path and "
                        "no metadata.gate_bypass — reaper will reopen this"
                    ),
                )
            )
    else:
        shas = _GIT_SHA_RE.findall(artifact_path)
        if artifact_path and not shas and not _is_truthy(
            metadata.get("evidence.doc_only")
        ):
            # evidence.artifact_path is set but contains no git:<sha> entry,
            # and nobody declared this is intentionally non-code evidence.
            # Previously this was silently "acceptable" — but a doc/test path
            # with no explicit doc_only flag means the bead's underlying code
            # claim (if any) was never checked. Force an explicit opt-in.
            result.violations.append(
                Violation(
                    bead_id=bead_id,
                    rule="doc_only_evidence",
                    detail=(
                        f"evidence.artifact_path={artifact_path!r} has no "
                        "git:<sha> entry, and metadata.evidence.doc_only is "
                        "not set. If this bead's evidence is intentionally "
                        "non-code (a writeup/research artifact with no "
                        "shipped code), set metadata.evidence.doc_only=true. "
                        "Otherwise add a git:<sha> entry for the commit that "
                        "actually ships the change."
                    ),
                )
            )

        for sha in shas:
            result.shas_checked.append(sha)
            if not is_ancestor(sha, branch, repo_root):
                result.violations.append(
                    Violation(
                        bead_id=bead_id,
                        rule="unreachable_sha",
                        detail=(
                            f"git:{sha} is not an ancestor of {branch} — the "
                            "commit lives only on a feature branch. Merge to "
                            f"{branch} before closing, or this bead will "
                            "reopen within an hour."
                        ),
                    )
                )

    # 4. Prose-cited SHAs (close_reason / notes) must independently be
    #    reachable. Only metadata.evidence.artifact_path is checked above —
    #    a closer can assert anything in free text, and until now nothing
    #    ever verified it. This is what let codeprobe-3cs/codeprobe-1gg close
    #    with "Merged to main @94d357c ... Published/reachable" in
    #    close_reason while 94d357c was never an ancestor of main.
    prose = " ".join(t for t in (bead.get("close_reason"), bead.get("notes")) if t)
    for sha in extract_prose_shas(prose):
        if sha in result.shas_checked:
            continue  # already verified above via evidence.artifact_path
        result.shas_checked.append(sha)
        if not is_ancestor(sha, branch, repo_root):
            result.violations.append(
                Violation(
                    bead_id=bead_id,
                    rule="unreachable_prose_sha",
                    detail=(
                        f"close_reason/notes cites {sha!r} as merged/"
                        f"reachable, but it is not an ancestor of {branch}. "
                        "Free-text SHA claims are not evidence — mirror the "
                        "real landing commit into "
                        "metadata.evidence.artifact_path=git:<sha> instead."
                    ),
                )
            )

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _find_repo_root(start: Path) -> Path | None:
    """Walk upward from ``start`` to find a directory containing ``.git``."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _format_text(checks: list[BeadCheck]) -> str:
    """Render the prescriptive-error text format. Quiet on success."""
    lines: list[str] = []
    for c in checks:
        if not c.violations:
            continue
        lines.append(f"\n[{c.bead_id}] ✗ {len(c.violations)} violation(s):")
        for v in c.violations:
            lines.append(f"  - [{v.rule}] {v.detail}")
    if not lines:
        return ""
    lines.append("")  # trailing newline
    lines.append(
        "See CLAUDE.md 'MANDATORY: Bead Close Ritual' for the close protocol. "
        "Fix violations before running `bd update --status=closed`."
    )
    return "\n".join(lines)


def _format_json(checks: list[BeadCheck]) -> str:
    payload = {
        "ok": all(not c.violations for c in checks),
        "beads": [
            {
                "bead_id": c.bead_id,
                "shas_checked": c.shas_checked,
                "bypass_legitimate": c.bypass_legitimate,
                "violations": [v.to_dict() for v in c.violations],
            }
            for c in checks
        ],
    }
    return json.dumps(payload, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n", 1)[0] if __doc__ else None,
    )
    parser.add_argument(
        "bead_ids",
        nargs="*",
        help="Bead IDs to check (e.g. codeprobe-jf28). Mutually exclusive with --epic.",
    )
    parser.add_argument(
        "--epic",
        help="Epic ID; check every child bead.",
    )
    parser.add_argument(
        "--branch",
        default="main",
        help="Target branch for reachability check (default: main).",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        help="Repo root (default: walk upward from cwd until .git is found).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON to stdout (for agent consumption).",
    )
    args = parser.parse_args(argv)

    if not args.bead_ids and not args.epic:
        parser.error("provide bead IDs or --epic")
    if args.bead_ids and args.epic:
        parser.error("--epic and positional bead IDs are mutually exclusive")

    repo_root = args.repo or _find_repo_root(Path.cwd())
    if repo_root is None:
        print(
            "error: could not locate a git repository (use --repo to override)",
            file=sys.stderr,
        )
        return 2

    if args.epic:
        bead_ids = fetch_epic_children(args.epic)
        if not bead_ids:
            print(
                f"error: epic {args.epic} has no children or could not be fetched",
                file=sys.stderr,
            )
            return 2
    else:
        bead_ids = args.bead_ids

    checks = [check_bead(bid, args.branch, repo_root) for bid in bead_ids]

    if args.json:
        print(_format_json(checks))
    else:
        text = _format_text(checks)
        if text:
            print(text, file=sys.stderr)

    return 0 if all(not c.violations for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
