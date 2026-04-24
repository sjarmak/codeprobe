"""Tests for the acceptance criteria loader — eval_mode_required field.

Covers the ``eval_mode_required`` field addition to the Criterion dataclass
and loader parsing. This field allows criteria to declare that they require
a specific eval mode (e.g. ``full`` for real agent runs) and should be
skipped in lighter modes.
"""

from __future__ import annotations

from pathlib import Path

from acceptance.loader import Criterion, load_criteria

# ---------------------------------------------------------------------------
# Criterion dataclass — eval_mode_required field
# ---------------------------------------------------------------------------


def test_criterion_eval_mode_required_defaults_to_none() -> None:
    """Criterion.eval_mode_required defaults to None when not specified."""
    c = Criterion(
        id="TEST-001",
        description="test",
        tier="structural",
        check_type="noop",
        severity="high",
        prd_source="docs/prd/x.md",
    )
    assert c.eval_mode_required is None


def test_criterion_eval_mode_required_accepts_string() -> None:
    """Criterion.eval_mode_required stores a string when provided."""
    c = Criterion(
        id="TEST-001",
        description="test",
        tier="structural",
        check_type="noop",
        severity="high",
        prd_source="docs/prd/x.md",
        eval_mode_required="full",
    )
    assert c.eval_mode_required == "full"


# ---------------------------------------------------------------------------
# Loader — parsing eval_mode_required from TOML
# ---------------------------------------------------------------------------


def _write_toml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "criteria.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_loader_parses_eval_mode_required(tmp_path: Path) -> None:
    """Loader reads eval_mode_required from TOML and stores it on Criterion."""
    body = """
[[criterion]]
id = "TEST-001"
description = "needs full run"
tier = "behavioral"
check_type = "cli_exit_code"
severity = "high"
prd_source = "docs/prd/x.md"
eval_mode_required = "full"
"""
    criteria = load_criteria(_write_toml(tmp_path, body))
    assert len(criteria) == 1
    assert criteria[0].eval_mode_required == "full"


def test_loader_defaults_eval_mode_required_to_none(tmp_path: Path) -> None:
    """When eval_mode_required is absent from TOML, Criterion gets None."""
    body = """
[[criterion]]
id = "TEST-001"
description = "no mode requirement"
tier = "structural"
check_type = "regex_present"
severity = "high"
prd_source = "docs/prd/x.md"
"""
    criteria = load_criteria(_write_toml(tmp_path, body))
    assert len(criteria) == 1
    assert criteria[0].eval_mode_required is None


def test_loader_mixed_eval_mode_required(tmp_path: Path) -> None:
    """Manifest with a mix of eval_mode_required and unset values."""
    body = """
[[criterion]]
id = "A-001"
description = "structural, no mode"
tier = "structural"
check_type = "regex_present"
severity = "high"
prd_source = "docs/prd/x.md"

[[criterion]]
id = "B-001"
description = "behavioral, needs full"
tier = "behavioral"
check_type = "cli_exit_code"
severity = "high"
prd_source = "docs/prd/x.md"
eval_mode_required = "full"
"""
    criteria = load_criteria(_write_toml(tmp_path, body))
    by_id = {c.id: c for c in criteria}
    assert by_id["A-001"].eval_mode_required is None
    assert by_id["B-001"].eval_mode_required == "full"


# ---------------------------------------------------------------------------
# Live manifest — criteria with eval_mode_required are present
# ---------------------------------------------------------------------------


def test_live_manifest_has_eval_mode_required_criteria() -> None:
    """At least some criteria in the real manifest declare eval_mode_required."""
    criteria = load_criteria()
    with_mode = [c for c in criteria if c.eval_mode_required is not None]
    assert len(with_mode) >= 1, "expected at least 1 criterion with eval_mode_required"
