#!/usr/bin/env python3
"""Partition two configs' real results into disjoint task subsets in place.

Used only to construct the E2E harness's incomparable-arms negative case:
takes the results of a real, completed run (both arms scored on the same
task set — genuine agent-produced telemetry, nothing fabricated) and
trims each config's ``completed`` list to a non-overlapping task-id
subset, so ``codeprobe interpret`` on the result sees two arms with zero
shared tasks and must REFUSE a verdict (codeprobe-f7rl.8).

Must run with the harness venv's Python (imports ``codeprobe.core
.experiment``, which is only installed there, not in this repo's own dev
environment).

Usage:
    python scripts/e2e/split_disjoint_results.py <exp_dir> \\
        --config-a armA --keep-a t1,t2 \\
        --config-b armB --keep-b t3,t4,t5
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from codeprobe.core.experiment import load_config_results, save_config_results


def split_disjoint_results(
    exp_dir: Path,
    config_a: str,
    keep_a: set[str],
    config_b: str,
    keep_b: set[str],
) -> None:
    if keep_a & keep_b:
        raise ValueError(
            f"keep_a and keep_b must be disjoint, got overlap: {keep_a & keep_b}"
        )
    for label, keep in ((config_a, keep_a), (config_b, keep_b)):
        results = load_config_results(exp_dir, label)
        trimmed = [t for t in results.completed if t.task_id in keep]
        if not trimmed:
            raise ValueError(
                f"filtering config {label!r} to {keep!r} left zero completed "
                f"tasks — check the requested ids against the real results"
            )
        save_config_results(exp_dir, label, trimmed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("exp_dir", type=Path)
    parser.add_argument("--config-a", required=True)
    parser.add_argument("--keep-a", required=True, help="Comma-separated task ids.")
    parser.add_argument("--config-b", required=True)
    parser.add_argument("--keep-b", required=True, help="Comma-separated task ids.")
    args = parser.parse_args(argv)

    try:
        split_disjoint_results(
            args.exp_dir,
            args.config_a,
            set(args.keep_a.split(",")),
            args.config_b,
            set(args.keep_b.split(",")),
        )
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"OK: split results in {args.exp_dir} into disjoint subsets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
