"""Tests for ``codeprobe check-infra drift``.

Bead: r4-tool-benefit-metadata. Acceptance criterion 4 tracked here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli.check_infra import check_infra
from codeprobe.mcp.capabilities import CAPABILITIES


def _write_metadata_json(
    task_dir: Path, capabilities_snapshot: list[str]
) -> Path:
    """Write a minimal metadata.json with a controlled capability snapshot."""
    payload = {
        "id": "t1",
        "repo": "r",
        "metadata": {
            "name": "t",
            "mcp_capabilities_at_mine_time": capabilities_snapshot,
        },
    }
    metadata_path = task_dir / "metadata.json"
    metadata_path.write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    return metadata_path


def test_matching_snapshot_exits_zero(tmp_path: Path) -> None:
    _write_metadata_json(tmp_path, sorted(CAPABILITIES.keys()))
    # --no-json keeps the legacy pretty "OK" surface; CliRunner is non-TTY
    # and would otherwise resolve to the envelope default.
    result = CliRunner().invoke(
        check_infra, ["drift", str(tmp_path), "--no-json"]
    )
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_drift_exits_nonzero_and_names_differing_capability(
    tmp_path: Path,
) -> None:
    # Snapshot missing a real capability → live has an "added" capability.
    live_keys = sorted(CAPABILITIES.keys())
    assert live_keys, "CAPABILITIES registry is unexpectedly empty"
    snapshot = live_keys[:-1]  # drop the last one → drift
    dropped = live_keys[-1]
    _write_metadata_json(tmp_path, snapshot)

    result = CliRunner().invoke(check_infra, ["drift", str(tmp_path)])
    assert result.exit_code != 0
    # The message must name the capability that differs (acceptance
    # criterion 4). Click prints ClickException text on stderr, which the
    # runner captures into ``result.output`` by default.
    combined = result.output + (result.stderr_bytes or b"").decode(errors="replace")
    assert dropped in combined, f"Drift output did not name {dropped!r}: {combined!r}"


def test_allow_capability_drift_tolerates_mismatch(tmp_path: Path) -> None:
    live_keys = sorted(CAPABILITIES.keys())
    snapshot = live_keys[:-1]
    _write_metadata_json(tmp_path, snapshot)

    result = CliRunner().invoke(
        check_infra,
        ["drift", str(tmp_path), "--allow-capability-drift"],
    )
    assert result.exit_code == 0
    # WARNING text is emitted to stderr — CliRunner may fold it into output;
    # we don't require a specific channel, just non-zero drift tolerated.


def test_extra_snapshot_capability_also_reports_drift(tmp_path: Path) -> None:
    """Snapshot contains a capability the live registry no longer has."""
    live_keys = sorted(CAPABILITIES.keys())
    snapshot = list(live_keys) + ["CAPABILITY_REMOVED_LATER"]
    _write_metadata_json(tmp_path, snapshot)

    result = CliRunner().invoke(check_infra, ["drift", str(tmp_path)])
    assert result.exit_code != 0
    combined = result.output + (result.stderr_bytes or b"").decode(errors="replace")
    assert "CAPABILITY_REMOVED_LATER" in combined


def test_missing_metadata_json_errors_cleanly(tmp_path: Path) -> None:
    result = CliRunner().invoke(check_infra, ["drift", str(tmp_path)])
    assert result.exit_code != 0
    assert "metadata.json" in (result.output or "")


def test_malformed_metadata_json_errors_cleanly(tmp_path: Path) -> None:
    (tmp_path / "metadata.json").write_text("{not json", encoding="utf-8")
    result = CliRunner().invoke(check_infra, ["drift", str(tmp_path)])
    assert result.exit_code != 0


@pytest.mark.parametrize(
    "bad_snapshot",
    [
        "not-a-list",
        [1, 2, 3],
        {"dict": "shape"},
    ],
)
def test_malformed_snapshot_field_errors_cleanly(
    tmp_path: Path, bad_snapshot: object
) -> None:
    payload = {
        "id": "t1",
        "repo": "r",
        "metadata": {
            "name": "t",
            "mcp_capabilities_at_mine_time": bad_snapshot,
        },
    }
    (tmp_path / "metadata.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    result = CliRunner().invoke(check_infra, ["drift", str(tmp_path)])
    assert result.exit_code != 0
