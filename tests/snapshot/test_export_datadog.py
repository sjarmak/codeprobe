"""AC1: ``snapshot export --format datadog`` emits a Datadog-ingestable artefact.

The intake envelope shape is ``{series: [...], events: [...]}`` with
``metric/points/tags`` on each series entry and ``title/text/tags`` on each
event. We assert the full structure, check that label tags are attached,
and verify the CLI writes the output to disk.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from codeprobe.cli import main as cli_main
from tests.snapshot._export_helpers import SYNTHETIC_ENTRIES, build_snapshot


def test_datadog_export_produces_valid_intake_envelope(tmp_path: Path) -> None:
    snap = build_snapshot(tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["snapshot", "export", str(snap), "--format", "datadog"],
    )
    assert result.exit_code == 0, result.output

    out_path = snap / "datadog.json"
    assert out_path.is_file()

    doc = json.loads(out_path.read_text())
    assert "series" in doc and isinstance(doc["series"], list)
    assert "events" in doc and isinstance(doc["events"], list)

    # One event per synthetic entry.
    assert len(doc["events"]) == len(SYNTHETIC_ENTRIES)

    # Every series entry has the mandatory fields.
    for s in doc["series"]:
        assert isinstance(s["metric"], str) and s["metric"].startswith("codeprobe.")
        assert isinstance(s["points"], list) and len(s["points"]) == 1
        ts, value = s["points"][0]
        assert isinstance(ts, int)
        assert isinstance(value, (int, float))
        assert isinstance(s["tags"], list)
        assert any(t.startswith("config:") for t in s["tags"])
        assert any(t.startswith("task_id:") for t in s["tags"])

    # Every event has mandatory fields.
    for e in doc["events"]:
        assert isinstance(e["title"], str) and "codeprobe task" in e["title"]
        assert isinstance(e["text"], str)
        assert isinstance(e["tags"], list)
        assert any(t.startswith("config:") for t in e["tags"])

    # Series coverage: each numeric field (score, cost_usd, latency_ms,
    # passed) becomes its own metric per entry.
    metrics = {s["metric"] for s in doc["series"]}
    assert "codeprobe.score" in metrics
    assert "codeprobe.cost_usd" in metrics
    assert "codeprobe.latency_ms" in metrics
    assert "codeprobe.passed" in metrics


def test_datadog_export_respects_explicit_out_path(tmp_path: Path) -> None:
    snap = build_snapshot(tmp_path)
    target = tmp_path / "outbox" / "dd.json"

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "snapshot",
            "export",
            str(snap),
            "--format",
            "datadog",
            "--out",
            str(target),
        ],
    )
    assert result.exit_code == 0, result.output
    assert target.is_file()
    doc = json.loads(target.read_text())
    assert set(doc.keys()) == {"series", "events"}
