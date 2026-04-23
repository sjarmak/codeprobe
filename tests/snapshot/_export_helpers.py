"""Shared helpers for snapshot exporter tests.

Every exporter test needs the same fixture shape: a small CSB-layout
snapshot on disk with a populated ``summary/aggregate.json`` containing a
handful of synthetic task rows. Centralising the builder keeps the tests
focused on their assertions rather than setup boilerplate.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from codeprobe.cli import main as cli_main


SYNTHETIC_ENTRIES: list[dict[str, object]] = [
    {
        "config": "baseline",
        "task_id": "task-001",
        "passed": True,
        "score": 0.91,
        "cost_usd": 0.012,
        "latency_ms": 1450,
    },
    {
        "config": "baseline",
        "task_id": "task-002",
        "passed": False,
        "score": 0.14,
        "cost_usd": 0.021,
        "latency_ms": 3100,
    },
    {
        "config": "mcp-on",
        "task_id": "task-001",
        "passed": True,
        "score": 0.97,
        "cost_usd": 0.019,
        "latency_ms": 1620,
    },
]


def build_snapshot(tmp_path: Path) -> Path:
    """Create a minimal snapshot at ``tmp_path/snap`` and return the path.

    The snapshot is produced by invoking ``codeprobe snapshot create`` on a
    trivial source tree, then we overwrite ``summary/aggregate.json`` with
    known synthetic entries so exporter assertions are deterministic.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "hello.txt").write_text("hello world\n")

    out = tmp_path / "snap"

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["snapshot", "create", str(src), "--out", str(out)],
    )
    assert result.exit_code == 0, result.output

    aggregate = out / "summary" / "aggregate.json"
    aggregate.parent.mkdir(parents=True, exist_ok=True)
    aggregate.write_text(
        json.dumps({"entries": SYNTHETIC_ENTRIES}, indent=2, sort_keys=True)
    )

    return out
