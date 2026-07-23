#!/usr/bin/env python3
"""Thin CLI wrapper around :class:`acceptance.release.ReleaseGate`.

Runs ``ReleaseGate(repo_root).build_and_stage()`` — build a wheel, install it
into a fresh venv, run ``codeprobe --version``, and exercise five structural
acceptance criteria against the freshly-staged install — and exits nonzero
unless every step succeeded.

This is the CI-facing half of the release gate. ``ReleaseGate.check_ready``
(the acceptance-loop verdict-history check) deliberately stays out of CI:
``verdict.json`` history is a local acceptance-loop artifact a fresh CI
runner does not have. The release skill runs ``check_ready`` locally before
tagging; this script runs in the ``gate`` job after the tag is pushed, as a
second, independent confirmation that the tagged tree actually builds and
installs cleanly.

Usage:
    python scripts/release_gate.py
    python scripts/release_gate.py --repo-root /path/to/repo

Exit codes:
    0   built, installed, version matched, structural smoke passed
    1   any StagingResult field is False — details printed to stderr
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT_DEFAULT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT_DEFAULT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT_DEFAULT))

from acceptance.release import ReleaseGate, StagingResult  # noqa: E402


def run_release_gate(repo_root: Path) -> StagingResult:
    return ReleaseGate(repo_root).build_and_stage()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build, stage, and smoke-test the wheel via ReleaseGate."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT_DEFAULT,
        help="Repo root (default: parent of scripts/).",
    )
    args = parser.parse_args(argv)

    result = run_release_gate(args.repo_root)
    print(
        f"built={result.built} installed={result.installed} "
        f"version_matches={result.version_matches} "
        f"structural_criteria_passed={result.structural_criteria_passed}"
    )
    if result.wheel_path is not None:
        print(f"wheel_path={result.wheel_path}")

    ok = (
        result.built
        and result.installed
        and result.version_matches
        and result.structural_criteria_passed
    )
    if not ok:
        print(f"error={result.error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
