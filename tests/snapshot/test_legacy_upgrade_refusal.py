"""Upgrade refusal for pre-hardening hashes-only snapshots."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

from click.testing import CliRunner

import codeprobe.cli.snapshot_cmd as snapshot_cmd
from codeprobe.cli import main
from codeprobe.snapshot.safe_io import inventory_tree
from codeprobe.snapshot.verify import unsafe_legacy_snapshot_reason


def _unsafe_legacy_snapshot(tmp_path: Path) -> Path:
    snapshot = tmp_path / "snapshot"
    body = snapshot / "export" / "traces" / "tasks" / "task-1" / "instruction.md"
    body.parent.mkdir(parents=True)
    body.write_text("proprietary source", encoding="utf-8")
    (snapshot / "SNAPSHOT.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "codeprobe_version": "0.11.0",
                "mode": "hashes-only",
                "files": [],
            }
        ),
        encoding="utf-8",
    )
    return snapshot


def test_detects_hashes_only_snapshot_that_contains_legacy_bodies(
    tmp_path: Path,
) -> None:
    snapshot = _unsafe_legacy_snapshot(tmp_path)

    reason = unsafe_legacy_snapshot_reason(snapshot)

    assert reason is not None
    assert "recreate" in reason
    assert "0.11.0" not in reason


def test_snapshot_verify_returns_prescriptive_upgrade_refusal(tmp_path: Path) -> None:
    snapshot = _unsafe_legacy_snapshot(tmp_path)

    inventory = Mock(wraps=inventory_tree)
    verifier_globals = snapshot_cmd.verify_snapshot_extended.__globals__
    with patch.dict(verifier_globals, {"inventory_tree": inventory}):
        result = CliRunner().invoke(
            main,
            ["snapshot", "verify", str(snapshot), "--json"],
        )

    assert result.exit_code == 2
    assert inventory.call_count == 1
    output_lines = result.stdout.strip().splitlines()
    assert len(output_lines) == 1
    assert str(snapshot) not in result.stdout
    assert "proprietary source" not in result.stdout
    envelope = json.loads(output_lines[0])
    assert envelope["error"]["code"] == "SNAPSHOT_UNSAFE_LEGACY_FORMAT"
    assert envelope["error"]["kind"] == "diagnostic"
    assert "snapshot create" in envelope["error"]["message_for_agent"]


def test_snapshot_verify_inventories_nonlegacy_tree_once(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "SNAPSHOT.json").write_text("{}", encoding="utf-8")

    inventory = Mock(wraps=inventory_tree)
    verifier_globals = snapshot_cmd.verify_snapshot_extended.__globals__
    with patch.dict(verifier_globals, {"inventory_tree": inventory}):
        result = CliRunner().invoke(
            main,
            ["snapshot", "verify", str(snapshot), "--json"],
        )

    assert result.exit_code == 2
    assert inventory.call_count == 1
