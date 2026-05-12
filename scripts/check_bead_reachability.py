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

This script is a STRUCTURAL check — it does not judge whether the
work was actually done. It only catches the two failure modes the
premortem identified for the evjr.* pattern (commits on feature
branches closed as shipped) and the predicted "gate_bypass-as-
release-valve" pattern.

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


@dataclass(frozen=True)
class Violation:
    """A single failed check on a single bead."""

    bead_id: str
    rule: str  # "unreachable_sha" | "banned_bypass" | "missing_evidence"
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
        return result

    shas = _GIT_SHA_RE.findall(artifact_path)
    if artifact_path and not shas:
        # evidence.artifact_path is set but contains no git:<sha> entries.
        # Acceptable — could be a docs/ path or a test fixture. Nothing to
        # check on reachability; not a violation.
        return result

    for sha in shas:
        result.shas_checked.append(sha)
        if not is_ancestor(sha, branch, repo_root):
            result.violations.append(
                Violation(
                    bead_id=bead_id,
                    rule="unreachable_sha",
                    detail=(
                        f"git:{sha} is not an ancestor of {branch} — the commit "
                        f"lives only on a feature branch. Merge to {branch} "
                        f"before closing, or this bead will reopen within an hour."
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
