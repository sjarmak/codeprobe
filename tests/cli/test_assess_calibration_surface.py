"""Tests that ``codeprobe assess`` surfaces a ``calibration_confidence``
field in its output.

Acceptance criterion 4: ``codeprobe assess`` output includes a
``calibration_confidence`` field. When a valid calibration profile is
available (via ``CODEPROBE_CALIBRATION_PROFILE`` env var), the value is the
correlation coefficient; when unavailable, the field is explicitly marked
``unavailable`` so downstream tooling can always rely on its presence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeprobe.calibration import (
    CalibrationProfile,
    format_calibration_line,
)
from codeprobe.cli.assess_cmd import (
    CALIBRATION_PROFILE_ENV,
    load_calibration_profile,
)


def _write_valid_profile(tmp_path: Path, correlation: float = 0.78) -> Path:
    profile = CalibrationProfile(
        correlation_coefficient=correlation,
        holdout_size=120,
        holdout_repos=("repo_a", "repo_b", "repo_c"),
        produced_at="2026-04-22T00:00:00+00:00",
        curator_version="surface-test-v1",
    )
    file = tmp_path / "profile.json"
    file.write_text(json.dumps(profile.to_dict()))
    return file


class TestFormatCalibrationLine:
    def test_includes_calibration_confidence_keyword(self) -> None:
        profile = CalibrationProfile(
            correlation_coefficient=0.72,
            holdout_size=150,
            holdout_repos=("a", "b", "c"),
            produced_at="2026-04-22T00:00:00+00:00",
            curator_version="v1",
        )
        line = format_calibration_line(profile)
        assert "calibration_confidence" in line
        assert "0.720" in line

    def test_unavailable_when_no_profile(self) -> None:
        line = format_calibration_line(None)
        assert "calibration_confidence" in line
        assert "unavailable" in line


class TestLoadCalibrationProfile:
    def test_loads_from_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        file = _write_valid_profile(tmp_path)
        monkeypatch.setenv(CALIBRATION_PROFILE_ENV, str(file))
        profile = load_calibration_profile()
        assert profile is not None
        assert profile.correlation_coefficient == pytest.approx(0.78)
        assert profile.holdout_size == 120

    def test_returns_none_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(CALIBRATION_PROFILE_ENV, raising=False)
        assert load_calibration_profile() is None

    def test_returns_none_when_path_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            CALIBRATION_PROFILE_ENV, str(tmp_path / "does-not-exist.json")
        )
        assert load_calibration_profile() is None

    def test_returns_none_on_malformed_json(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        file = tmp_path / "bad.json"
        file.write_text("not json {")
        monkeypatch.setenv(CALIBRATION_PROFILE_ENV, str(file))
        assert load_calibration_profile() is None

    def test_returns_none_on_missing_fields(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        file = tmp_path / "partial.json"
        file.write_text(json.dumps({"correlation_coefficient": 0.7}))
        monkeypatch.setenv(CALIBRATION_PROFILE_ENV, str(file))
        assert load_calibration_profile() is None


# ---------------------------------------------------------------------------
# End-to-end: codeprobe assess surface
# ---------------------------------------------------------------------------


class TestAssessSurface:
    def test_assess_prints_calibration_confidence_when_profile_set(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify the surfacing mechanism without invoking the full assess CLI.

        We stub ``assess_repo`` to avoid requiring a real git repo, then call
        ``run_assess`` and capture stdout.
        """
        from click.testing import CliRunner

        from codeprobe.assess.heuristics import AssessmentScore, DimensionScore

        file = _write_valid_profile(tmp_path, correlation=0.73)
        monkeypatch.setenv(CALIBRATION_PROFILE_ENV, str(file))

        fake_score = AssessmentScore(
            overall=0.6,
            recommendation="Good candidate",
            dimensions=(
                DimensionScore(name="task_richness", score=0.6, reasoning="ok"),
            ),
            scoring_method="heuristic",
            model_used=None,
        )

        # Patch assess_repo to return a fixed score; also stub the git
        # repo check by creating a fake .git dir under tmp_path.
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(
            "codeprobe.assess.assess_repo", lambda _p: fake_score
        )

        runner = CliRunner()
        from codeprobe.cli import main

        result = runner.invoke(main, ["assess", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "calibration_confidence" in result.output
        assert "0.730" in result.output

    def test_assess_surfaces_unavailable_when_no_profile(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from click.testing import CliRunner

        from codeprobe.assess.heuristics import AssessmentScore, DimensionScore

        monkeypatch.delenv(CALIBRATION_PROFILE_ENV, raising=False)

        fake_score = AssessmentScore(
            overall=0.3,
            recommendation="Needs more work",
            dimensions=(
                DimensionScore(name="task_richness", score=0.3, reasoning="thin"),
            ),
            scoring_method="heuristic",
            model_used=None,
        )

        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(
            "codeprobe.assess.assess_repo", lambda _p: fake_score
        )

        runner = CliRunner()
        from codeprobe.cli import main

        result = runner.invoke(main, ["assess", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "calibration_confidence" in result.output
        assert "unavailable" in result.output
