"""Tests for codeprobe.assess — heuristics gathering and scoring."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from codeprobe.assess.heuristics import (
    AssessmentScore,
    RepoHeuristics,
    assess_repo,
    gather_heuristics,
    score_repo,
)


# ---------------------------------------------------------------------------
# Helpers to build RepoHeuristics with sensible defaults
# ---------------------------------------------------------------------------


def _make_heuristics(**overrides: object) -> RepoHeuristics:
    defaults: dict[str, object] = {
        "total_commits": 0,
        "merge_commits": 0,
        "contributors": 1,
        "has_ci": False,
        "has_tests": False,
        "test_frameworks": (),
        "primary_languages": (),
        "total_files": 5,
        "repo_age_days": 10,
        "recent_activity": False,
    }
    merged = {**defaults, **overrides}
    # Ensure list values are converted to tuples for frozen dataclass
    for key in ("test_frameworks", "primary_languages"):
        if isinstance(merged[key], list):
            merged[key] = tuple(merged[key])
    return RepoHeuristics(**merged)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# score_repo unit tests
# ---------------------------------------------------------------------------


class TestScoreRepoExcellent:
    """Excellent repos should score >= 0.7."""

    def test_score_repo_excellent(self) -> None:
        h = _make_heuristics(
            total_commits=600,
            merge_commits=100,
            contributors=5,
            has_ci=True,
            has_tests=True,
            test_frameworks=["pytest"],
            primary_languages=["Python"],
            total_files=200,
            repo_age_days=365,
            recent_activity=True,
        )
        score = score_repo(h)
        assert score.overall >= 0.7
        assert score.task_richness == 1.0
        assert score.test_coverage == 1.0
        assert score.complexity == 1.0
        assert score.activity == 1.0
        assert "Excellent" in score.recommendation


class TestScoreRepoGood:
    """Moderate repos should score between 0.5 and 0.7."""

    def test_score_repo_good(self) -> None:
        h = _make_heuristics(
            total_commits=150,
            merge_commits=25,
            contributors=2,
            has_ci=True,
            has_tests=True,
            test_frameworks=[],
            primary_languages=["JavaScript"],
            total_files=60,
            repo_age_days=180,
            recent_activity=True,
        )
        score = score_repo(h)
        assert 0.5 <= score.overall < 0.7
        assert "Good" in score.recommendation


class TestScoreRepoPoor:
    """Minimal repos should score < 0.3."""

    def test_score_repo_poor(self) -> None:
        h = _make_heuristics(
            total_commits=5,
            merge_commits=0,
            contributors=1,
            has_ci=False,
            has_tests=False,
            test_frameworks=[],
            primary_languages=[],
            total_files=3,
            repo_age_days=2,
            recent_activity=False,
        )
        score = score_repo(h)
        assert score.overall < 0.3
        assert "Poor" in score.recommendation


class TestScoreRepoNoTests:
    """Repos with no tests should have test_coverage == 0.0."""

    def test_score_repo_no_tests(self) -> None:
        h = _make_heuristics(has_tests=False, has_ci=False, test_frameworks=[])
        score = score_repo(h)
        assert score.test_coverage == 0.0


class TestScoreRepoRecommendationText:
    """Verify recommendation strings match score ranges."""

    @pytest.mark.parametrize(
        "merge_commits, has_tests, has_ci, fw, files, commits, contributors, recent, expected_word",
        [
            (100, True, True, ["pytest"], 200, 600, 5, True, "Excellent"),
            (25, True, True, [], 60, 150, 2, True, "Good"),
            (5, True, False, [], 20, 50, 1, False, "Fair"),
            (0, False, False, [], 3, 5, 1, False, "Poor"),
        ],
    )
    def test_recommendation_matches_range(
        self,
        merge_commits: int,
        has_tests: bool,
        has_ci: bool,
        fw: list[str],
        files: int,
        commits: int,
        contributors: int,
        recent: bool,
        expected_word: str,
    ) -> None:
        h = _make_heuristics(
            merge_commits=merge_commits,
            has_tests=has_tests,
            has_ci=has_ci,
            test_frameworks=fw,
            total_files=files,
            total_commits=commits,
            contributors=contributors,
            recent_activity=recent,
        )
        score = score_repo(h)
        assert expected_word in score.recommendation


class TestScoreRepoWeightsSumToOne:
    """Verify scoring weights sum to 1.0."""

    def test_weights_sum_to_one(self) -> None:
        # Create a heuristics where every signal scores 1.0
        h = _make_heuristics(
            merge_commits=50,
            has_tests=True,
            has_ci=True,
            test_frameworks=["pytest"],
            total_files=100,
            total_commits=500,
            contributors=3,
            recent_activity=True,
        )
        score = score_repo(h)
        # If all sub-scores are 1.0 and weights sum to 1.0, overall must be 1.0
        assert score.task_richness == 1.0
        assert score.test_coverage == 1.0
        assert score.complexity == 1.0
        assert score.activity == 1.0
        assert abs(score.overall - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# gather_heuristics — mocked subprocess
# ---------------------------------------------------------------------------


class TestGatherHeuristicsMockSubprocess:
    """Mock all subprocess calls and verify parsing."""

    def test_gather_heuristics_mock_subprocess(self) -> None:
        repo = Path("/fake/repo")

        def fake_run(args: list[str], **kwargs: object) -> object:
            """Return realistic git output based on the command."""

            class FakeResult:
                def __init__(self, stdout: str) -> None:
                    self.stdout = stdout
                    self.returncode = 0

            cmd = args[1] if len(args) > 1 else ""
            rest = args[2:] if len(args) > 2 else []

            if cmd == "rev-list" and "--merges" in rest:
                return FakeResult("12\n")
            if cmd == "rev-list" and "--count" in rest:
                return FakeResult("250\n")
            if cmd == "shortlog":
                return FakeResult("  100\tAlice\n   80\tBob\n   30\tCharlie\n")
            if cmd == "ls-files" and "--" in rest:
                # Test glob queries for test files
                return FakeResult("")
            if cmd == "ls-files":
                return FakeResult("src/main.py\nsrc/utils.py\ntests/test_main.py\nREADME.md\n")
            if cmd == "log" and "--reverse" in rest:
                return FakeResult("2023-01-01T00:00:00+00:00")
            if cmd == "log" and "--since=30.days" in rest:
                return FakeResult("abc1234 recent commit")
            if cmd == "log":
                return FakeResult("2024-06-15T00:00:00+00:00")
            return FakeResult("")

        with (
            patch("codeprobe.assess.heuristics.subprocess.run", side_effect=fake_run),
            patch.object(Path, "is_dir", return_value=False),
            patch.object(Path, "is_file", return_value=False),
            patch.object(Path, "exists", return_value=False),
        ):
            h = gather_heuristics(repo)

        assert h.total_commits == 250
        assert h.merge_commits == 12
        assert h.contributors == 3
        assert h.total_files == 4
        assert h.recent_activity is True
        assert h.repo_age_days == 531  # 2023-01-01 to 2024-06-15
        assert "Python" in h.primary_languages


# ---------------------------------------------------------------------------
# gather_heuristics — real repo (integration test)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestGatherHeuristicsRealRepo:
    """Run gather_heuristics against the codeprobe repo itself."""

    def test_gather_heuristics_real_repo(self) -> None:
        # Resolve the codeprobe repo root (this file is in tests/)
        repo_root = Path(__file__).resolve().parent.parent
        h = gather_heuristics(repo_root)

        # Basic sanity: the repo exists and has commits
        assert h.total_commits >= 0
        assert h.total_files >= 0
        assert isinstance(h.primary_languages, tuple)
        assert isinstance(h.has_ci, bool)
        assert isinstance(h.has_tests, bool)
        assert isinstance(h.repo_age_days, int)
        assert h.repo_age_days >= 0
        assert isinstance(h.recent_activity, bool)
        assert isinstance(h.contributors, int)
        assert isinstance(h.test_frameworks, tuple)


# ---------------------------------------------------------------------------
# assess_repo pipeline
# ---------------------------------------------------------------------------


class TestAssessRepoPipeline:
    """Mock gather_heuristics and verify score_repo is called."""

    def test_assess_repo_pipeline(self) -> None:
        fake_h = _make_heuristics(
            total_commits=300,
            merge_commits=30,
            contributors=4,
            has_ci=True,
            has_tests=True,
            test_frameworks=["jest"],
            total_files=80,
            repo_age_days=200,
            recent_activity=True,
        )
        with patch("codeprobe.assess.heuristics.gather_heuristics", return_value=fake_h) as mock_gh:
            result = assess_repo(Path("/any/path"))
            mock_gh.assert_called_once_with(Path("/any/path"))

        assert isinstance(result, AssessmentScore)
        assert 0.0 <= result.overall <= 1.0
        assert result.recommendation
