"""Tests for the acceptance criteria manifest and loader.

Covers every acceptance criterion from the ``criteria-manifest`` work unit:

- TOML manifest is parseable by :mod:`tomllib` and has >= 25 entries.
- Each criterion has all required fields.
- Tier counts meet the minima (8 structural, 12 behavioral, 5 statistical).
- Loader validates required fields and raises :class:`ValueError` on
  malformed criteria.
- Loader supports filtering by tier and severity.
- Dependency DAG is resolved via topological sort and cycles raise
  :class:`ValueError`.
- Manifest covers the known integration-test bugs, silent-pass-through
  detection, telemetry completeness, scoring correctness, logging verbosity,
  and error-handling contracts.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from acceptance.loader import (
    ALLOWED_SEVERITIES,
    ALLOWED_TIERS,
    REQUIRED_FIELDS,
    Criterion,
    filter_by_severity,
    filter_by_tier,
    load_criteria,
    topological_sort,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "acceptance" / "criteria.toml"


# ---------------------------------------------------------------------------
# Manifest shape — raw TOML assertions
# ---------------------------------------------------------------------------


def test_manifest_file_exists() -> None:
    assert MANIFEST_PATH.is_file(), f"manifest missing at {MANIFEST_PATH}"


def test_manifest_parses_with_tomllib() -> None:
    with MANIFEST_PATH.open("rb") as fh:
        data = tomllib.load(fh)
    assert "criterion" in data
    assert isinstance(data["criterion"], list)


def test_manifest_has_at_least_25_entries() -> None:
    with MANIFEST_PATH.open("rb") as fh:
        data = tomllib.load(fh)
    assert (
        len(data["criterion"]) >= 25
    ), f"Only {len(data['criterion'])} criteria defined; need >= 25"


def test_manifest_every_entry_has_required_fields() -> None:
    with MANIFEST_PATH.open("rb") as fh:
        data = tomllib.load(fh)
    for i, entry in enumerate(data["criterion"]):
        missing = [f for f in REQUIRED_FIELDS if not entry.get(f)]
        assert not missing, (
            f"criterion[{i}] id={entry.get('id', '<missing>')} "
            f"missing required fields: {missing}"
        )


# ---------------------------------------------------------------------------
# Loader — happy path
# ---------------------------------------------------------------------------


def test_load_criteria_returns_at_least_25() -> None:
    criteria = load_criteria()
    assert len(criteria) >= 25


def test_load_criteria_returns_frozen_dataclasses() -> None:
    criteria = load_criteria()
    c = criteria[0]
    assert isinstance(c, Criterion)
    with pytest.raises(Exception):
        # frozen dataclass — attribute assignment must fail
        c.id = "MUTATED"  # type: ignore[misc]


def test_load_criteria_unique_ids() -> None:
    criteria = load_criteria()
    ids = [c.id for c in criteria]
    assert len(ids) == len(set(ids))


def test_load_criteria_every_item_has_required_fields() -> None:
    criteria = load_criteria()
    for c in criteria:
        for field_name in REQUIRED_FIELDS:
            assert getattr(
                c, field_name
            ), f"criterion {c.id} has empty required field {field_name}"


def test_load_criteria_tier_values_valid() -> None:
    for c in load_criteria():
        assert c.tier in ALLOWED_TIERS


def test_load_criteria_severity_values_valid() -> None:
    for c in load_criteria():
        assert c.severity in ALLOWED_SEVERITIES


# ---------------------------------------------------------------------------
# Tier distribution — 8 structural + 12 behavioral + 5 statistical
# ---------------------------------------------------------------------------


def test_at_least_8_structural_criteria() -> None:
    criteria = load_criteria()
    structural = filter_by_tier(criteria, "structural")
    assert len(structural) >= 8, f"expected >= 8 structural, got {len(structural)}"


def test_at_least_12_behavioral_criteria() -> None:
    criteria = load_criteria()
    behavioral = filter_by_tier(criteria, "behavioral")
    assert len(behavioral) >= 12, f"expected >= 12 behavioral, got {len(behavioral)}"


def test_at_least_5_statistical_criteria() -> None:
    criteria = load_criteria()
    statistical = filter_by_tier(criteria, "statistical")
    assert len(statistical) >= 5, f"expected >= 5 statistical, got {len(statistical)}"


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def test_filter_by_tier_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        filter_by_tier(load_criteria(), "nonsense")


def test_filter_by_severity_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        filter_by_severity(load_criteria(), "nonsense")


def test_filter_by_severity_returns_subset() -> None:
    criteria = load_criteria()
    critical = filter_by_severity(criteria, "critical")
    assert all(c.severity == "critical" for c in critical)
    assert len(critical) >= 1


# ---------------------------------------------------------------------------
# Coverage of mandated areas
# ---------------------------------------------------------------------------


def _ids() -> set[str]:
    return {c.id for c in load_criteria()}


def test_covers_six_integration_test_bugs() -> None:
    ids = _ids()
    expected_prefixes = {
        "BUG-MINE-RUN",
        "BUG-OUT-FLAG",
        "BUG-INTERPRET-STDOUT",
        "BUG-INTERPRET-EXIT",
        "BUG-VALIDATE-DISCOVERY",
        "BUG-INIT-DEFAULT",
    }
    for prefix in expected_prefixes:
        assert any(
            cid.startswith(prefix) for cid in ids
        ), f"missing bug criterion with prefix {prefix}"


def test_covers_silent_pass_through_detection() -> None:
    ids = _ids()
    for prefix in ("SILENT-MINE-COUNT", "SILENT-RUN-RESULTS", "SILENT-CANARY"):
        assert any(cid.startswith(prefix) for cid in ids), prefix


def test_covers_telemetry_completeness() -> None:
    ids = _ids()
    for prefix in ("TELEM-COST-SOURCE", "TELEM-COST-USD", "TELEM-TOKENS"):
        assert any(cid.startswith(prefix) for cid in ids), prefix


def test_covers_scoring_correctness() -> None:
    ids = _ids()
    for prefix in (
        "SCORE-PASS-THRESHOLD",
        "SCORE-FILE-LIST-F1",
        "SCORE-TESTSH-EXIT",
    ):
        assert any(cid.startswith(prefix) for cid in ids), prefix


def test_covers_error_handling() -> None:
    ids = _ids()
    for prefix in (
        "ERR-TIMEOUT-CAT",
        "ERR-FALLBACK-LOG",
        "ERR-NO-SILENT-ZERO",
    ):
        assert any(cid.startswith(prefix) for cid in ids), prefix


def test_covers_logging_verbosity_levels() -> None:
    ids = _ids()
    for prefix in ("LOG-VERBOSE-DEBUG", "LOG-QUIET-WARNING", "LOG-STDERR"):
        assert any(cid.startswith(prefix) for cid in ids), prefix


# ---------------------------------------------------------------------------
# Loader — validation (malformed manifests)
# ---------------------------------------------------------------------------


def _write_toml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "criteria.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_missing_required_field_raises(tmp_path: Path) -> None:
    body = """
[[criterion]]
id = "X-001"
# description deliberately missing
tier = "structural"
check_type = "noop"
severity = "high"
prd_source = "docs/prd/x.md"
"""
    with pytest.raises(ValueError, match="missing required fields"):
        load_criteria(_write_toml(tmp_path, body))


def test_unknown_tier_raises(tmp_path: Path) -> None:
    body = """
[[criterion]]
id = "X-001"
description = "bad tier"
tier = "bogus"
check_type = "noop"
severity = "high"
prd_source = "docs/prd/x.md"
"""
    with pytest.raises(ValueError, match="unknown tier"):
        load_criteria(_write_toml(tmp_path, body))


def test_unknown_severity_raises(tmp_path: Path) -> None:
    body = """
[[criterion]]
id = "X-001"
description = "bad severity"
tier = "structural"
check_type = "noop"
severity = "whatever"
prd_source = "docs/prd/x.md"
"""
    with pytest.raises(ValueError, match="unknown severity"):
        load_criteria(_write_toml(tmp_path, body))


def test_duplicate_ids_raise(tmp_path: Path) -> None:
    body = """
[[criterion]]
id = "DUP-1"
description = "first"
tier = "structural"
check_type = "noop"
severity = "high"
prd_source = "docs/prd/x.md"

[[criterion]]
id = "DUP-1"
description = "second"
tier = "structural"
check_type = "noop"
severity = "high"
prd_source = "docs/prd/x.md"
"""
    with pytest.raises(ValueError, match="[Dd]uplicate"):
        load_criteria(_write_toml(tmp_path, body))


def test_unresolved_dependency_raises(tmp_path: Path) -> None:
    body = """
[[criterion]]
id = "A-001"
description = "depends on missing"
tier = "behavioral"
check_type = "noop"
severity = "high"
prd_source = "docs/prd/x.md"
depends_on = ["NOPE"]
"""
    with pytest.raises(ValueError, match="unknown criteria"):
        load_criteria(_write_toml(tmp_path, body))


def test_self_dependency_raises(tmp_path: Path) -> None:
    body = """
[[criterion]]
id = "A-001"
description = "self cycle"
tier = "behavioral"
check_type = "noop"
severity = "high"
prd_source = "docs/prd/x.md"
depends_on = ["A-001"]
"""
    with pytest.raises(ValueError, match="itself"):
        load_criteria(_write_toml(tmp_path, body))


def test_cyclic_dependencies_raise(tmp_path: Path) -> None:
    body = """
[[criterion]]
id = "A-001"
description = "a"
tier = "behavioral"
check_type = "noop"
severity = "high"
prd_source = "docs/prd/x.md"
depends_on = ["B-001"]

[[criterion]]
id = "B-001"
description = "b"
tier = "behavioral"
check_type = "noop"
severity = "high"
prd_source = "docs/prd/x.md"
depends_on = ["A-001"]
"""
    with pytest.raises(ValueError, match="[Cc]ycle"):
        load_criteria(_write_toml(tmp_path, body))


def test_empty_manifest_raises(tmp_path: Path) -> None:
    body = "# no criteria here\n"
    with pytest.raises(ValueError, match="non-empty"):
        load_criteria(_write_toml(tmp_path, body))


def test_missing_manifest_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_criteria(tmp_path / "does_not_exist.toml")


# ---------------------------------------------------------------------------
# Topological sort
# ---------------------------------------------------------------------------


def test_topological_sort_respects_dependencies() -> None:
    criteria = load_criteria()
    position = {c.id: i for i, c in enumerate(criteria)}
    for c in criteria:
        for dep in c.depends_on:
            assert (
                position[dep] < position[c.id]
            ), f"criterion {c.id} appears before its dependency {dep}"


def test_topological_sort_preserves_all_criteria() -> None:
    criteria = load_criteria()
    sorted_again = topological_sort(list(criteria))
    assert len(sorted_again) == len(criteria)
    assert {c.id for c in sorted_again} == {c.id for c in criteria}


def test_topological_sort_detects_cycle_directly() -> None:
    # Build an in-memory cycle: A -> B -> A. We can't construct this through
    # load_criteria (it rejects earlier), so we hand-build Criterion objects.
    a = Criterion(
        id="A",
        description="a",
        tier="behavioral",
        check_type="noop",
        severity="high",
        prd_source="docs/prd/x.md",
        depends_on=("B",),
    )
    b = Criterion(
        id="B",
        description="b",
        tier="behavioral",
        check_type="noop",
        severity="high",
        prd_source="docs/prd/x.md",
        depends_on=("A",),
    )
    with pytest.raises(ValueError, match="[Cc]ycle"):
        topological_sort([a, b])
