#!/usr/bin/env python3
"""Write one honest result through the installed prior CodeProbe release."""

from __future__ import annotations

import argparse
from pathlib import Path

from codeprobe.core.experiment import save_config_results
from codeprobe.models.experiment import CompletedTask


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", type=Path)
    args = parser.parse_args()
    tasks = sorted(path.name for path in (args.experiment / "tasks").iterdir())
    if len(tasks) != 1:
        raise RuntimeError("prior release must produce exactly one task")
    save_config_results(
        args.experiment,
        "baseline",
        [
            CompletedTask(
                task_id=tasks[0],
                automated_score=1.0,
                duration_seconds=1.0,
                input_tokens=10,
                output_tokens=5,
                cost_usd=0.01,
                cost_model="upgrade-fixture",
                cost_source="measured",
            )
        ],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
