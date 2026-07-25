#!/usr/bin/env python3
"""CI entry point for the complete acceptance release gate.

Loads the tracked, version-bound acceptance verdicts, calls
``ReleaseGate.check_ready()``, and only then calls
``ReleaseGate.build_and_stage()``. The command exits nonzero unless both
verdicts are ready and every staging result is true.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT_DEFAULT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT_DEFAULT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT_DEFAULT))

from acceptance.release import ReleaseGate, StagingResult  # noqa: E402
from acceptance.release_evidence import (  # noqa: E402
    ReleaseEvidenceError,
    load_release_evidence,
)


def run_release_gate(
    repo_root: Path,
    verdict_paths: list[Path],
) -> tuple[bool, StagingResult | None]:
    """Run readiness and staging through one ``ReleaseGate`` instance."""
    gate = ReleaseGate(repo_root)
    if not gate.check_ready(verdict_paths):
        return False, None
    return True, gate.build_and_stage()


def _staging_succeeded(result: StagingResult) -> bool:
    return (
        result.built
        and result.installed
        and result.version_matches
        and result.structural_criteria_passed
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate acceptance verdicts, then build and stage the wheel."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT_DEFAULT,
        help="Repo root (default: parent of scripts/).",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        required=True,
        help="Tracked release-verdict evidence directory.",
    )
    parser.add_argument(
        "--expected-version",
        required=True,
        help="Release version expected in the evidence manifest.",
    )
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    evidence_dir = args.evidence_dir
    if not evidence_dir.is_absolute():
        evidence_dir = repo_root / evidence_dir

    try:
        verdict_paths = load_release_evidence(
            evidence_dir.resolve(),
            args.expected_version,
        )
    except ReleaseEvidenceError as exc:
        print(f"release evidence rejected: {exc}", file=sys.stderr)
        return 1

    ready, result = run_release_gate(repo_root, verdict_paths)
    if not ready or result is None:
        print(
            "release evidence rejected: the two acceptance verdicts are not "
            "both EVALUATED + all_pass",
            file=sys.stderr,
        )
        return 1

    print(
        f"built={result.built} installed={result.installed} "
        f"version_matches={result.version_matches} "
        f"structural_criteria_passed={result.structural_criteria_passed}"
    )
    if result.wheel_path is not None:
        print(f"wheel_path={result.wheel_path}")
    if not _staging_succeeded(result):
        print(f"error={result.error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
