#!/usr/bin/env python3
"""Validate retained real-agent journey evidence for one release candidate."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from acceptance.enterprise_journey import (  # noqa: E402
    EnterpriseJourneyEvidenceError,
    validate_enterprise_journey_evidence,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-agent-image", required=True)
    parser.add_argument("--expected-scoring-image", required=True)
    parser.add_argument("--max-cost-usd", required=True, type=float)
    args = parser.parse_args(argv)
    try:
        wheel_digest = _sha256(args.wheel)
        evidence = validate_enterprise_journey_evidence(
            args.evidence,
            expected_version=args.expected_version,
            expected_commit=args.expected_commit,
            expected_wheel_sha256=wheel_digest,
            expected_agent_image=args.expected_agent_image,
            expected_scoring_image=args.expected_scoring_image,
            max_cost_usd=args.max_cost_usd,
        )
    except OSError:
        print(
            "enterprise release evidence rejected: candidate artifact cannot be read",
            file=sys.stderr,
        )
        return 1
    except EnterpriseJourneyEvidenceError as exc:
        print(f"enterprise release evidence rejected: {exc}", file=sys.stderr)
        return 1
    print(
        "enterprise release evidence accepted: "
        f"version={evidence['candidate']['version']} "
        f"producer={evidence['producer']['agent']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
