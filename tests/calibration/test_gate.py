"""Tests for the R11 calibration gate.

Synthetic holdout data only — partner data is explicitly out of scope for
this unit (see docs/CALIBRATION.md).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.calibration import (
    CalibrationProfile,
    CalibrationRejected,
    HoldoutRow,
    compute_pearson,
    emit_profile,
    load_holdout,
    refuse_profile_emission,
    validate_calibration_correlation,
)
from codeprobe.cli import main

# ---------------------------------------------------------------------------
# Synthetic holdout generators
# ---------------------------------------------------------------------------

NUM_REPOS = 5
PER_REPO = 25  # 5 * 25 = 125 rows (>= R11 min_tasks of 100)


def _linear_holdout(
    *,
    slope: float = 1.0,
    intercept: float = 0.0,
    noise: float = 0.0,
    n_repos: int = NUM_REPOS,
    per_repo: int = PER_REPO,
) -> tuple[HoldoutRow, ...]:
    """Deterministic synthetic holdout.

    Uses a reproducible ``random.Random`` with a fixed seed so every call
    with identical parameters produces identical rows. No floating-point
    clock or system randomness leaks in.
    """
    import random

    rng = random.Random(42)
    rows: list[HoldoutRow] = []
    for repo_idx in range(n_repos):
        repo_name = f"repo_{repo_idx}"
        for task_idx in range(per_repo):
            # Curator A scores spread evenly across [0, 1].
            a = (task_idx + 1) / (per_repo + 1)
            # Curator B is a linear function of A with optional noise.
            b = slope * a + intercept + rng.uniform(-noise, noise)
            rows.append(
                HoldoutRow(
                    task_id=f"{repo_name}__task_{task_idx}",
                    curator_a=a,
                    curator_b=b,
                    repo=repo_name,
                )
            )
    return tuple(rows)


def _target_correlation_holdout(
    target_r: float,
    *,
    n_repos: int = NUM_REPOS,
    per_repo: int = PER_REPO,
) -> tuple[HoldoutRow, ...]:
    """Generate a holdout whose Pearson correlation is close to ``target_r``.

    Uses ``y = target_r * x + sqrt(1 - target_r^2) * z`` where ``z`` is an
    independent Gaussian. With fixed-seed RNG this is deterministic.
    """
    import random

    rng = random.Random(target_r)  # seed from the target so each r is stable
    rows: list[HoldoutRow] = []
    for repo_idx in range(n_repos):
        repo_name = f"repo_{repo_idx}"
        for task_idx in range(per_repo):
            x = rng.gauss(0.5, 0.2)
            z = rng.gauss(0.0, 0.2)
            y = target_r * x + math.sqrt(max(0.0, 1.0 - target_r * target_r)) * z
            rows.append(
                HoldoutRow(
                    task_id=f"{repo_name}__task_{task_idx}",
                    curator_a=x,
                    curator_b=y,
                    repo=repo_name,
                )
            )
    return tuple(rows)


# ---------------------------------------------------------------------------
# compute_pearson
# ---------------------------------------------------------------------------


class TestComputePearson:
    def test_perfect_correlation(self) -> None:
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [2.0, 4.0, 6.0, 8.0, 10.0]
        assert compute_pearson(xs, ys) == pytest.approx(1.0)

    def test_perfect_negative_correlation(self) -> None:
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [5.0, 4.0, 3.0, 2.0, 1.0]
        assert compute_pearson(xs, ys) == pytest.approx(-1.0)

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(CalibrationRejected, match="equal-length"):
            compute_pearson([1.0, 2.0], [1.0])

    def test_too_few_observations_raises(self) -> None:
        with pytest.raises(CalibrationRejected, match="at least 2"):
            compute_pearson([1.0], [1.0])

    def test_zero_variance_raises(self) -> None:
        with pytest.raises(CalibrationRejected, match="zero variance"):
            compute_pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0])


# ---------------------------------------------------------------------------
# refuse_profile_emission
# ---------------------------------------------------------------------------


class TestRefuseProfileEmission:
    def test_accepts_large_multi_repo_holdout(self) -> None:
        rows = _linear_holdout()
        # Should not raise.
        refuse_profile_emission(rows)

    def test_rejects_too_few_tasks(self) -> None:
        rows = _linear_holdout(n_repos=3, per_repo=10)  # 30 rows
        with pytest.raises(CalibrationRejected, match="too small"):
            refuse_profile_emission(rows)

    def test_rejects_too_few_repos(self) -> None:
        rows = _linear_holdout(n_repos=2, per_repo=60)  # 120 rows, 2 repos
        with pytest.raises(CalibrationRejected, match="too few repos"):
            refuse_profile_emission(rows)

    def test_custom_thresholds(self) -> None:
        rows = _linear_holdout(n_repos=1, per_repo=5)
        # Tighten thresholds: accept tiny holdouts explicitly.
        refuse_profile_emission(rows, min_tasks=5, min_repos=1)


# ---------------------------------------------------------------------------
# validate_calibration_correlation
# ---------------------------------------------------------------------------


def _make_profile(correlation: float) -> CalibrationProfile:
    return CalibrationProfile(
        correlation_coefficient=correlation,
        holdout_size=150,
        holdout_repos=("repo_a", "repo_b", "repo_c"),
        produced_at="2026-04-22T00:00:00+00:00",
        curator_version="test-v1",
    )


class TestValidateCalibrationCorrelation:
    def test_accepts_above_threshold(self) -> None:
        validate_calibration_correlation(_make_profile(0.7))

    def test_accepts_exactly_at_threshold(self) -> None:
        validate_calibration_correlation(_make_profile(0.6))

    def test_rejects_below_threshold(self) -> None:
        with pytest.raises(CalibrationRejected, match="below threshold"):
            validate_calibration_correlation(_make_profile(0.5))

    def test_custom_threshold(self) -> None:
        # Profile at 0.7 should fail a stricter 0.8 threshold.
        with pytest.raises(CalibrationRejected):
            validate_calibration_correlation(_make_profile(0.7), threshold=0.8)


# ---------------------------------------------------------------------------
# emit_profile (acceptance criterion 3)
# ---------------------------------------------------------------------------


class TestEmitProfile:
    def test_emits_at_target_correlation_0_7(self) -> None:
        """Synthetic holdout targeting r=0.7 passes the 0.6 gate."""
        rows = _target_correlation_holdout(0.7)
        profile = emit_profile(rows, curator_version="v1", threshold=0.6)
        assert profile.correlation_coefficient >= 0.6
        assert profile.holdout_size == len(rows)
        assert len(profile.holdout_repos) >= 3

    def test_rejects_at_target_correlation_0_5(self) -> None:
        """Synthetic holdout targeting r=0.5 is refused by the 0.6 gate."""
        rows = _target_correlation_holdout(0.5)
        with pytest.raises(CalibrationRejected):
            emit_profile(rows, curator_version="v1", threshold=0.6)

    def test_rejects_small_holdout_before_computing_correlation(self) -> None:
        rows = _linear_holdout(n_repos=3, per_repo=10)  # 30 rows
        with pytest.raises(CalibrationRejected, match="too small"):
            emit_profile(rows, curator_version="v1")

    def test_records_distinct_repos_sorted(self) -> None:
        rows = _target_correlation_holdout(0.9)
        profile = emit_profile(rows, curator_version="v1")
        assert profile.holdout_repos == tuple(sorted(profile.holdout_repos))


# ---------------------------------------------------------------------------
# load_holdout
# ---------------------------------------------------------------------------


class TestLoadHoldout:
    def test_roundtrip(self, tmp_path: Path) -> None:
        payload = [
            {"task_id": "t1", "curator_a": 0.8, "curator_b": 0.75, "repo": "r1"},
            {"task_id": "t2", "curator_a": 0.4, "curator_b": 0.35, "repo": "r2"},
        ]
        file = tmp_path / "holdout.json"
        file.write_text(json.dumps(payload))
        rows = load_holdout(file)
        assert len(rows) == 2
        assert rows[0].task_id == "t1"
        assert rows[1].repo == "r2"

    def test_missing_file_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(CalibrationRejected, match="does not exist"):
            load_holdout(tmp_path / "missing.json")

    def test_bad_json_rejected(self, tmp_path: Path) -> None:
        file = tmp_path / "bad.json"
        file.write_text("not valid json {")
        with pytest.raises(CalibrationRejected, match="invalid"):
            load_holdout(file)

    def test_missing_fields_rejected(self, tmp_path: Path) -> None:
        file = tmp_path / "missing_fields.json"
        file.write_text(json.dumps([{"task_id": "t1"}]))
        with pytest.raises(CalibrationRejected, match="missing fields"):
            load_holdout(file)

    def test_non_list_rejected(self, tmp_path: Path) -> None:
        file = tmp_path / "not_list.json"
        file.write_text(json.dumps({"wrong": "shape"}))
        with pytest.raises(CalibrationRejected, match="must be a list"):
            load_holdout(file)


# ---------------------------------------------------------------------------
# CLI: codeprobe calibrate
# ---------------------------------------------------------------------------


def _write_holdout(tmp_path: Path, rows: tuple[HoldoutRow, ...]) -> Path:
    payload = [
        {
            "task_id": r.task_id,
            "curator_a": r.curator_a,
            "curator_b": r.curator_b,
            "repo": r.repo,
        }
        for r in rows
    ]
    file = tmp_path / "holdout.json"
    file.write_text(json.dumps(payload))
    return file


def _combined_output(result: object) -> str:
    """Return stdout+stderr for a CliRunner result (click 8.3 API)."""
    parts = [getattr(result, "output", "")]
    try:
        parts.append(result.stderr)  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    return "".join(parts)


class TestCalibrateCli:
    def test_cli_emits_profile_on_pass(self, tmp_path: Path) -> None:
        rows = _target_correlation_holdout(0.9)
        holdout = _write_holdout(tmp_path, rows)
        out = tmp_path / "profile.json"

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "calibrate",
                str(holdout),
                "--curator-version",
                "cli-test-v1",
                "--out",
                str(out),
            ],
        )
        assert result.exit_code == 0, _combined_output(result)

        # Profile JSON is written to --out; parse that directly (stdout is
        # the same JSON but we'd have to strip any progress lines).
        assert out.exists(), "Expected profile file to be written"
        payload = json.loads(out.read_text())
        assert payload["calibration_confidence"] >= 0.6
        assert "correlation_coefficient" in payload
        assert payload["holdout_size"] == len(rows)
        assert len(payload["holdout_repos"]) >= 3

    def test_cli_rejects_low_correlation(self, tmp_path: Path) -> None:
        rows = _target_correlation_holdout(0.3)
        holdout = _write_holdout(tmp_path, rows)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "calibrate",
                str(holdout),
                "--curator-version",
                "cli-test-v1",
            ],
        )
        assert result.exit_code == 1
        assert "calibration_rejected" in _combined_output(result)

    def test_cli_rejects_small_holdout(self, tmp_path: Path) -> None:
        rows = _linear_holdout(n_repos=3, per_repo=10)  # 30 rows
        holdout = _write_holdout(tmp_path, rows)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "calibrate",
                str(holdout),
                "--curator-version",
                "cli-test-v1",
            ],
        )
        assert result.exit_code == 1
        assert "too small" in _combined_output(result)
