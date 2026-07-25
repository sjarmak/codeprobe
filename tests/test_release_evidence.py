"""Tests for the tracked release-evidence contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from acceptance.release_evidence import (
    ReleaseEvidenceError,
    export_release_evidence,
    load_release_evidence,
)


def _verdict(path: Path, iteration: int) -> Path:
    path.write_text(
        json.dumps(
            {
                "iteration": iteration,
                "status": "EVALUATED",
                "all_pass": True,
            }
        )
    )
    return path


def test_exported_evidence_round_trips(tmp_path: Path) -> None:
    verdicts = [
        _verdict(tmp_path / "verdict-0007.json", 7),
        _verdict(tmp_path / "verdict-0008.json", 8),
    ]
    evidence_dir = tmp_path / "evidence"

    export_release_evidence(verdicts, evidence_dir, "1.2.3")

    loaded = load_release_evidence(evidence_dir, "1.2.3")
    assert [json.loads(path.read_text())["iteration"] for path in loaded] == [7, 8]


def test_release_evidence_rejects_wrong_release_version(tmp_path: Path) -> None:
    verdicts = [
        _verdict(tmp_path / "verdict-0007.json", 7),
        _verdict(tmp_path / "verdict-0008.json", 8),
    ]
    evidence_dir = tmp_path / "evidence"
    export_release_evidence(verdicts, evidence_dir, "1.2.3")

    with pytest.raises(ReleaseEvidenceError, match="not bound"):
        load_release_evidence(evidence_dir, "1.2.4")


def test_release_evidence_rejects_modified_verdict(tmp_path: Path) -> None:
    verdicts = [
        _verdict(tmp_path / "verdict-0007.json", 7),
        _verdict(tmp_path / "verdict-0008.json", 8),
    ]
    evidence_dir = tmp_path / "evidence"
    export_release_evidence(verdicts, evidence_dir, "1.2.3")
    (evidence_dir / "verdict-latest.json").write_text("{}")

    with pytest.raises(ReleaseEvidenceError, match="SHA-256"):
        load_release_evidence(evidence_dir, "1.2.3")
