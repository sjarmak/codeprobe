"""Tests for codeprobe trace export — JSONL output."""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli.trace_cmd import trace
from codeprobe.trace.content_policy import ContentPolicy
from codeprobe.trace.recorder import TraceRecorder
from codeprobe.trace.store import export_jsonl


def _make_trace(db: Path, n: int) -> None:
    policy = ContentPolicy(env_values=frozenset(), deny_globs=())
    with TraceRecorder(db, run_id="r", content_policy=policy, batch_size=1) as rec:
        for i in range(n):
            rec.record_event(
                config="c",
                task_id="t",
                event_type="tool_use",
                tool_name=f"Tool{i}",
                tool_input=f"input-{i}",
            )


@pytest.mark.unit
def test_export_jsonl_row_count_matches(tmp_path: Path) -> None:
    """Acceptance (8) — JSONL row count equals the SQLite row count."""
    db = tmp_path / "trace.db"
    _make_trace(db, 12)

    sql_count = sqlite3.connect(str(db)).execute(
        "SELECT COUNT(*) FROM events"
    ).fetchone()[0]

    buf = io.StringIO()
    exported = export_jsonl(db, buf)

    assert exported == sql_count
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    assert len(lines) == sql_count


@pytest.mark.unit
def test_export_preserves_redaction(tmp_path: Path) -> None:
    """A redacted row shows the redaction marker in JSONL, not the secret."""
    db = tmp_path / "trace.db"
    secret_value = "super-secret-unique-token-12345"
    policy = ContentPolicy(env_values=frozenset({secret_value}), deny_globs=())
    with TraceRecorder(db, run_id="r", content_policy=policy, batch_size=1) as rec:
        rec.record_event(
            config="c",
            task_id="t",
            event_type="tool_use",
            tool_input=f"plain text and {secret_value} here",
        )

    buf = io.StringIO()
    export_jsonl(db, buf)
    text = buf.getvalue()
    assert secret_value not in text
    assert "[REDACTED-ENV]" in text


@pytest.mark.unit
def test_export_cli_writes_file(tmp_path: Path) -> None:
    db_dir = tmp_path / "run-root"
    db_dir.mkdir()
    db = db_dir / "trace.db"
    _make_trace(db, 5)

    out_path = tmp_path / "out.jsonl"
    runner = CliRunner()
    result = runner.invoke(
        trace,
        ["export", str(db_dir), "--output", str(out_path)],
    )
    assert result.exit_code == 0, result.output
    assert out_path.is_file()
    lines = out_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    # Each line is valid JSON.
    for ln in lines:
        obj = json.loads(ln)
        assert obj["run_id"] == "r"
        assert obj["event_type"] == "tool_use"


@pytest.mark.unit
def test_export_cli_missing_db(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(trace, ["export", str(empty_dir)])
    assert result.exit_code != 0
    assert "No trace.db" in result.output


@pytest.mark.integration
def test_planted_secret_not_in_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance (5) — a planted env secret does NOT appear in any row of trace.db."""
    secret = "s3cr3t-codeprobe-test-value-must-not-leak"
    monkeypatch.setenv("CODEPROBE_TEST_SECRET", secret)

    db = tmp_path / "trace.db"
    # Rebuild policy AFTER setting the env so the snapshot picks up the secret.
    policy = ContentPolicy()
    assert secret in policy.env_values

    with TraceRecorder(db, run_id="r", content_policy=policy, batch_size=1) as rec:
        rec.record_event(
            config="c",
            task_id="t",
            event_type="tool_use",
            tool_input=f"agent received token: {secret} and proceeded",
            tool_output=f"wrote: {secret}",
        )

    # SQL scan: no row contains the literal secret in any column.
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT tool_input, tool_output FROM events"
        ).fetchall()
    finally:
        conn.close()
    for tin, tout in rows:
        assert tin is None or secret not in tin
        assert tout is None or secret not in tout
