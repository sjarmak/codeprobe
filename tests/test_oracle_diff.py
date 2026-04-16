"""Tests for codeprobe.assess.oracle_diff — flavor B and C validation.

These tests use synthetic fixtures so they run fast and deterministically.
Real-oracle integration (CSB MANIFEST.json, EB task shapes) is out of scope
per the 2026-04-13 scope pivot documented on bead codeprobe-y67.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from codeprobe.assess.oracle_diff import (
    CheckOutcome,
    FlavorResult,
    flavor_b_from_csb_manifest,
    flavor_b_score_correlation,
    flavor_c_e2e_divergence,
)

# ---------------------------------------------------------------------------
# FlavorResult / CheckOutcome dataclass contracts
# ---------------------------------------------------------------------------


def test_flavor_result_is_frozen() -> None:
    result = FlavorResult(
        status="pass",
        flavor="B",
        oracle="codescalebench",
        artifact_dir=Path("/tmp/does-not-exist"),
        checks=(),
        summary={},
    )
    with pytest.raises((AttributeError, Exception)):
        result.status = "fail"  # type: ignore[misc]


def test_check_outcome_is_frozen() -> None:
    outcome = CheckOutcome(name="x", passed=True, detail="ok")
    with pytest.raises((AttributeError, Exception)):
        outcome.passed = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Flavor B — synthetic paired-score CSV correlation
# ---------------------------------------------------------------------------


def _write_paired_csv(path: Path, rows: list[tuple[str, float, float, str]]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["task_id", "codeprobe_score", "oracle_score", "oracle_source"])
        for r in rows:
            writer.writerow(r)


def test_flavor_b_perfect_correlation(tmp_path: Path) -> None:
    csv_path = tmp_path / "paired.csv"
    # codeprobe and oracle scores are identical — perfect correlation expected.
    rows = [
        (f"t-{i:03d}", float(i) / 10.0, float(i) / 10.0, "csb") for i in range(1, 11)
    ]
    _write_paired_csv(csv_path, rows)

    artifact_dir = tmp_path / "artifact"
    result = flavor_b_score_correlation(
        paired_scores_csv=csv_path,
        min_correlation=0.7,
        artifact_dir=artifact_dir,
    )

    assert result.status == "pass"
    assert result.flavor == "B"
    assert result.summary["n_tasks"] == 10
    # Spearman and Kendall on strictly monotonic data are 1.0
    assert abs(result.summary["spearman"] - 1.0) < 1e-6
    assert abs(result.summary["kendall"] - 1.0) < 1e-6

    # Artifacts exist with expected shapes
    corr = json.loads((artifact_dir / "correlation.json").read_text())
    assert abs(corr["spearman"] - 1.0) < 1e-6
    assert corr["n_tasks"] == 10

    summary = json.loads((artifact_dir / "summary.json").read_text())
    assert summary["status"] == "pass"
    assert summary["flavor"] == "B"

    outliers_md = (artifact_dir / "outliers.md").read_text()
    assert "Outliers" in outliers_md or "outliers" in outliers_md.lower()


def test_flavor_b_fails_threshold(tmp_path: Path) -> None:
    csv_path = tmp_path / "paired.csv"
    # Anti-correlated codeprobe/oracle — Spearman ≈ -1.0
    rows = [
        (f"t-{i:03d}", float(i) / 10.0, float(11 - i) / 10.0, "csb")
        for i in range(1, 11)
    ]
    _write_paired_csv(csv_path, rows)

    artifact_dir = tmp_path / "artifact"
    result = flavor_b_score_correlation(
        paired_scores_csv=csv_path,
        min_correlation=0.7,
        artifact_dir=artifact_dir,
    )
    assert result.status == "fail"
    assert result.summary["spearman"] < 0


def test_flavor_b_too_few_samples_fails(tmp_path: Path) -> None:
    csv_path = tmp_path / "paired.csv"
    rows = [("t-001", 0.5, 0.5, "csb"), ("t-002", 0.6, 0.6, "csb")]
    _write_paired_csv(csv_path, rows)

    artifact_dir = tmp_path / "artifact"
    result = flavor_b_score_correlation(
        paired_scores_csv=csv_path,
        min_correlation=0.7,
        artifact_dir=artifact_dir,
        min_n=5,
    )
    assert result.status == "fail"
    # Failure reason recorded in checks
    assert any("sample" in c.detail.lower() or "n=" in c.detail.lower() for c in result.checks)


def test_flavor_b_missing_csv_raises(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifact"
    with pytest.raises(FileNotFoundError):
        flavor_b_score_correlation(
            paired_scores_csv=tmp_path / "missing.csv",
            min_correlation=0.7,
            artifact_dir=artifact_dir,
        )


def test_flavor_b_outliers_ordered_by_gap(tmp_path: Path) -> None:
    csv_path = tmp_path / "paired.csv"
    # 10 well-correlated rows plus two big outliers
    rows: list[tuple[str, float, float, str]] = [
        (f"t-{i:03d}", 0.1 * i, 0.1 * i, "csb") for i in range(1, 11)
    ]
    rows.append(("outlier-big", 0.1, 0.9, "csb"))  # |gap|=0.8
    rows.append(("outlier-small", 0.3, 0.5, "csb"))  # |gap|=0.2
    _write_paired_csv(csv_path, rows)

    artifact_dir = tmp_path / "artifact"
    result = flavor_b_score_correlation(
        paired_scores_csv=csv_path,
        min_correlation=0.5,
        artifact_dir=artifact_dir,
    )
    assert result.flavor == "B"
    outliers_md = (artifact_dir / "outliers.md").read_text()
    big_idx = outliers_md.find("outlier-big")
    small_idx = outliers_md.find("outlier-small")
    assert big_idx != -1
    assert small_idx != -1
    assert big_idx < small_idx  # larger gap listed first


# ---------------------------------------------------------------------------
# Flavor B — CSB manifest extractor
# ---------------------------------------------------------------------------


def test_flavor_b_from_csb_manifest(tmp_path: Path) -> None:
    manifest = {
        "description": "test",
        "total_tasks": 3,
        "total_runs": 1,
        "runs": {
            "suite_a/run1": {
                "run_id": "suite_a_run1",
                "model": "claude-test",
                "task_count": 3,
                "tasks": {
                    "task-1": {"status": "passed", "reward": 0.8},
                    "task-2": {"status": "failed", "reward": 0.2},
                    "task-3": {"status": "passed", "reward": 1.0},
                },
            }
        },
    }
    manifest_path = tmp_path / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest))

    # Codeprobe scores dict: task_id -> score
    codeprobe_scores = {"task-1": 0.85, "task-2": 0.25, "task-3": 0.9, "missing": 0.5}

    output_csv = tmp_path / "paired.csv"
    n_rows = flavor_b_from_csb_manifest(
        manifest_path=manifest_path,
        codeprobe_scores=codeprobe_scores,
        output_csv=output_csv,
        run_filter="suite_a/run1",
    )
    assert n_rows == 3  # "missing" is not in the manifest, excluded

    with open(output_csv) as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert len(rows) == 3
    by_id = {r["task_id"]: r for r in rows}
    assert float(by_id["task-1"]["codeprobe_score"]) == 0.85
    assert float(by_id["task-1"]["oracle_score"]) == 0.8
    assert by_id["task-1"]["oracle_source"] == "suite_a/run1"


# ---------------------------------------------------------------------------
# Flavor C — E2E outcome divergence
# ---------------------------------------------------------------------------


def _write_outcomes_json(path: Path, outcomes: dict[str, str]) -> None:
    """Write outcomes as `{"task_id": "passed"/"failed"/"errored"}` JSON."""
    path.write_text(json.dumps(outcomes))


def test_flavor_c_all_match(tmp_path: Path) -> None:
    cp_path = tmp_path / "cp.json"
    or_path = tmp_path / "or.json"
    outcomes = {"t1": "passed", "t2": "failed", "t3": "passed"}
    _write_outcomes_json(cp_path, outcomes)
    _write_outcomes_json(or_path, outcomes)

    artifact_dir = tmp_path / "artifact"
    result = flavor_c_e2e_divergence(
        codeprobe_outcomes_json=cp_path,
        oracle_outcomes_json=or_path,
        artifact_dir=artifact_dir,
    )
    assert result.status == "pass"
    assert result.summary["match_rate"] == 1.0
    assert result.summary["n_tasks"] == 3

    with open(artifact_dir / "e2e_outcomes.csv") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert len(rows) == 3
    assert all(r["match"] == "true" for r in rows)


def test_flavor_c_detects_mismatch(tmp_path: Path) -> None:
    cp_path = tmp_path / "cp.json"
    or_path = tmp_path / "or.json"
    _write_outcomes_json(cp_path, {"t1": "passed", "t2": "failed", "t3": "passed"})
    _write_outcomes_json(or_path, {"t1": "passed", "t2": "passed", "t3": "failed"})

    artifact_dir = tmp_path / "artifact"
    result = flavor_c_e2e_divergence(
        codeprobe_outcomes_json=cp_path,
        oracle_outcomes_json=or_path,
        artifact_dir=artifact_dir,
        min_match_rate=0.8,
    )
    assert result.status == "fail"
    assert abs(result.summary["match_rate"] - (1.0 / 3.0)) < 1e-9

    with open(artifact_dir / "e2e_outcomes.csv") as f:
        reader = csv.DictReader(f)
        rows = {r["task_id"]: r for r in reader}
    assert rows["t1"]["match"] == "true"
    assert rows["t2"]["match"] == "false"
    assert rows["t3"]["match"] == "false"


def test_flavor_c_disjoint_sets_error(tmp_path: Path) -> None:
    cp_path = tmp_path / "cp.json"
    or_path = tmp_path / "or.json"
    _write_outcomes_json(cp_path, {"t1": "passed"})
    _write_outcomes_json(or_path, {"t2": "passed"})

    artifact_dir = tmp_path / "artifact"
    result = flavor_c_e2e_divergence(
        codeprobe_outcomes_json=cp_path,
        oracle_outcomes_json=or_path,
        artifact_dir=artifact_dir,
    )
    assert result.status == "fail"
    assert any(
        "no overlap" in c.detail.lower() or "disjoint" in c.detail.lower()
        for c in result.checks
    )


def test_flavor_c_partial_overlap(tmp_path: Path) -> None:
    cp_path = tmp_path / "cp.json"
    or_path = tmp_path / "or.json"
    _write_outcomes_json(cp_path, {"t1": "passed", "t2": "failed", "only-cp": "passed"})
    _write_outcomes_json(or_path, {"t1": "passed", "t2": "failed", "only-or": "passed"})

    artifact_dir = tmp_path / "artifact"
    result = flavor_c_e2e_divergence(
        codeprobe_outcomes_json=cp_path,
        oracle_outcomes_json=or_path,
        artifact_dir=artifact_dir,
    )
    # Only joined pair: t1, t2 — both match
    assert result.summary["n_tasks"] == 2
    assert result.summary["match_rate"] == 1.0
    # But unmatched ids recorded in summary
    assert result.summary["n_only_codeprobe"] == 1
    assert result.summary["n_only_oracle"] == 1


def test_flavor_c_accepts_list_form(tmp_path: Path) -> None:
    """Outcomes JSON may also be a list of {task_id, outcome} objects."""
    cp_path = tmp_path / "cp.json"
    or_path = tmp_path / "or.json"
    cp_path.write_text(
        json.dumps([{"task_id": "t1", "outcome": "passed"}, {"task_id": "t2", "outcome": "failed"}])
    )
    or_path.write_text(
        json.dumps([{"task_id": "t1", "outcome": "passed"}, {"task_id": "t2", "outcome": "passed"}])
    )

    artifact_dir = tmp_path / "artifact"
    result = flavor_c_e2e_divergence(
        codeprobe_outcomes_json=cp_path,
        oracle_outcomes_json=or_path,
        artifact_dir=artifact_dir,
    )
    assert result.summary["n_tasks"] == 2
    assert abs(result.summary["match_rate"] - 0.5) < 1e-9


def test_flavor_c_missing_file_raises(tmp_path: Path) -> None:
    or_path = tmp_path / "or.json"
    or_path.write_text(json.dumps({"t1": "passed"}))
    with pytest.raises(FileNotFoundError):
        flavor_c_e2e_divergence(
            codeprobe_outcomes_json=tmp_path / "missing.json",
            oracle_outcomes_json=or_path,
            artifact_dir=tmp_path / "artifact",
        )
