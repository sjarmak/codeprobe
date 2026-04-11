"""Tests for codeprobe.assess — heuristics gathering and scoring."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from codeprobe.assess.heuristics import (
    RUBRIC_V1,
    AssessmentScore,
    DimensionScore,
    RepoHeuristics,
    assess_repo,
    gather_heuristics,
    score_repo_heuristic,
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
        "has_docs": False,
    }
    merged = {**defaults, **overrides}
    # Ensure list values are converted to tuples for frozen dataclass
    for key in ("test_frameworks", "primary_languages"):
        if isinstance(merged[key], list):
            merged[key] = tuple(merged[key])
    return RepoHeuristics(**merged)  # type: ignore[arg-type]


def _dim_by_name(score: AssessmentScore, name: str) -> DimensionScore:
    """Get a DimensionScore by name from an AssessmentScore."""
    for d in score.dimensions:
        if d.name == name:
            return d
    raise KeyError(f"No dimension named {name!r}")


# Reusable "rich repo" heuristics for tests that need a realistic baseline.
_RICH_REPO = _make_heuristics(
    total_commits=300,
    merge_commits=30,
    contributors=4,
    has_ci=True,
    has_tests=True,
    test_frameworks=["jest"],
    total_files=80,
    recent_activity=True,
)


# ---------------------------------------------------------------------------
# score_repo_heuristic unit tests
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
        score = score_repo_heuristic(h)
        assert score.overall >= 0.7
        assert _dim_by_name(score, "task_richness").score == 1.0
        assert _dim_by_name(score, "test_coverage").score == 1.0
        assert _dim_by_name(score, "complexity").score == 1.0
        assert _dim_by_name(score, "activity").score == 1.0
        assert "Excellent" in score.recommendation
        assert score.scoring_method == "heuristic"
        assert score.model_used is None


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
        score = score_repo_heuristic(h)
        assert 0.4 <= score.overall < 0.75
        assert score.scoring_method == "heuristic"


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
        score = score_repo_heuristic(h)
        assert score.overall < 0.3
        assert "Poor" in score.recommendation


class TestScoreRepoNoTests:
    """Repos with no tests should have test_coverage == 0.0."""

    def test_score_repo_no_tests(self) -> None:
        h = _make_heuristics(has_tests=False, has_ci=False, test_frameworks=[])
        score = score_repo_heuristic(h)
        assert _dim_by_name(score, "test_coverage").score == 0.0


class TestScoreRepoRecommendationText:
    """Verify recommendation strings match score ranges."""

    @pytest.mark.parametrize(
        "merge_commits, has_tests, has_ci, fw, files, commits, contributors, recent, expected_word",
        [
            (100, True, True, ["pytest"], 200, 600, 5, True, "Excellent"),
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
        score = score_repo_heuristic(h)
        assert expected_word in score.recommendation


class TestScoreRepoAllDimensionsMaxed:
    """When all signals are maxed, overall should be close to 1.0."""

    def test_all_maxed(self) -> None:
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
        score = score_repo_heuristic(h)
        assert _dim_by_name(score, "task_richness").score == 1.0
        assert _dim_by_name(score, "test_coverage").score == 1.0
        assert _dim_by_name(score, "complexity").score == 1.0
        assert _dim_by_name(score, "activity").score == 1.0
        assert score.overall >= 0.8


# ---------------------------------------------------------------------------
# RUBRIC_V1 dimension coverage tests
# ---------------------------------------------------------------------------


class TestHeuristicReturnsAllRubricDimensions:
    """Heuristic path must return all RUBRIC_V1 dimensions."""

    def test_all_dimensions_present(self) -> None:
        h = _make_heuristics()
        score = score_repo_heuristic(h)
        dim_names = {d.name for d in score.dimensions}
        assert dim_names == set(RUBRIC_V1)

    def test_dimension_order_matches_rubric(self) -> None:
        h = _make_heuristics()
        score = score_repo_heuristic(h)
        assert tuple(d.name for d in score.dimensions) == RUBRIC_V1

    def test_all_dimensions_have_reasoning(self) -> None:
        h = _make_heuristics()
        score = score_repo_heuristic(h)
        for d in score.dimensions:
            assert d.reasoning, f"Dimension {d.name!r} has empty reasoning"

    def test_all_scores_in_range(self) -> None:
        h = _make_heuristics(
            total_commits=300,
            merge_commits=30,
            contributors=4,
            has_ci=True,
            has_tests=True,
            test_frameworks=["jest"],
            total_files=80,
        )
        score = score_repo_heuristic(h)
        for d in score.dimensions:
            assert (
                0.0 <= d.score <= 1.0
            ), f"Dimension {d.name!r} score out of range: {d.score}"


# ---------------------------------------------------------------------------
# Model path tests (mocked)
# ---------------------------------------------------------------------------


class TestModelPathReturnsAllRubricDimensions:
    """Model path must return all RUBRIC_V1 dimensions."""

    def test_model_dimensions_match_rubric(self) -> None:
        from codeprobe.assess.heuristics import _parse_model_assessment

        model_response = {
            "overall": 0.75,
            "recommendation": "Good benchmarking candidate",
            "dimensions": [
                {"name": name, "score": 0.8, "reasoning": f"Test reasoning for {name}"}
                for name in RUBRIC_V1
            ],
        }
        result = _parse_model_assessment(model_response, model_used="haiku", details={})
        dim_names = {d.name for d in result.dimensions}
        assert dim_names == set(RUBRIC_V1)
        assert result.scoring_method == "model"
        assert result.model_used == "haiku"

    def test_model_missing_dimension_raises(self) -> None:
        from codeprobe.assess.heuristics import _parse_model_assessment
        from codeprobe.core.llm import LLMParseError

        model_response = {
            "overall": 0.5,
            "recommendation": "Missing dims",
            "dimensions": [
                {"name": "task_richness", "score": 0.5, "reasoning": "ok"},
            ],
        }
        with pytest.raises(LLMParseError, match="missing dimensions"):
            _parse_model_assessment(model_response, model_used="haiku", details={})

    def test_model_missing_dimensions_key_raises(self) -> None:
        from codeprobe.assess.heuristics import _parse_model_assessment
        from codeprobe.core.llm import LLMParseError

        model_response = {"overall": 0.5, "recommendation": "No dims"}
        with pytest.raises(LLMParseError, match="dimensions"):
            _parse_model_assessment(model_response, model_used="haiku", details={})

    def test_model_duplicate_dimension_raises(self) -> None:
        from codeprobe.assess.heuristics import _parse_model_assessment
        from codeprobe.core.llm import LLMParseError

        model_response = {
            "overall": 0.5,
            "recommendation": "Dupes",
            "dimensions": [
                {"name": "task_richness", "score": 0.5, "reasoning": "first"},
                {"name": "task_richness", "score": 0.8, "reasoning": "second"},
            ],
        }
        with pytest.raises(LLMParseError, match="Duplicate dimension"):
            _parse_model_assessment(model_response, model_used="haiku", details={})


class TestExtractJson:
    """_extract_json strips markdown fences from model responses."""

    def test_plain_json_passthrough(self) -> None:
        from codeprobe.assess.heuristics import _extract_json

        raw = '{"overall": 0.5}'
        assert _extract_json(raw) == '{"overall": 0.5}'

    def test_strips_json_fence(self) -> None:
        from codeprobe.assess.heuristics import _extract_json

        raw = '```json\n{"overall": 0.5}\n```'
        assert _extract_json(raw) == '{"overall": 0.5}'

    def test_strips_bare_fence(self) -> None:
        from codeprobe.assess.heuristics import _extract_json

        raw = '```\n{"overall": 0.5}\n```'
        assert _extract_json(raw) == '{"overall": 0.5}'

    def test_strips_fence_with_whitespace(self) -> None:
        from codeprobe.assess.heuristics import _extract_json

        raw = '  ```json\n  {"overall": 0.5}\n  ```  '
        assert '"overall": 0.5' in _extract_json(raw)


class TestBothPathsSameShape:
    """Model and heuristic paths produce the same AssessmentScore shape."""

    def test_field_names_match(self) -> None:
        from codeprobe.assess.heuristics import _parse_model_assessment

        heuristic_score = score_repo_heuristic(_RICH_REPO)

        model_response = {
            "overall": 0.75,
            "recommendation": "Good candidate",
            "dimensions": [
                {"name": name, "score": 0.8, "reasoning": f"Reasoning for {name}"}
                for name in RUBRIC_V1
            ],
        }
        model_score = _parse_model_assessment(
            model_response, model_used="haiku", details={}
        )

        # Same top-level fields
        assert set(heuristic_score.__dataclass_fields__) == set(
            model_score.__dataclass_fields__
        )
        # Same dimension names in same order
        assert tuple(d.name for d in heuristic_score.dimensions) == tuple(
            d.name for d in model_score.dimensions
        )
        # Different scoring methods
        assert heuristic_score.scoring_method == "heuristic"
        assert model_score.scoring_method == "model"


# ---------------------------------------------------------------------------
# Fallback tests
# ---------------------------------------------------------------------------


class TestFallbackOnMissingBinary:
    """assess_repo falls back to heuristic when claude CLI missing."""

    def test_fallback_when_no_claude(self) -> None:
        with (
            patch(
                "codeprobe.assess.heuristics.gather_heuristics", return_value=_RICH_REPO
            ),
            patch("codeprobe.core.llm.claude_available", return_value=False),
        ):
            result = assess_repo(Path("/any/path"))

        assert result.scoring_method == "heuristic"
        assert result.model_used is None
        assert isinstance(result, AssessmentScore)


class TestFallbackOnParseError:
    """assess_repo falls back to heuristic when model call fails."""

    def test_fallback_on_llm_error(self) -> None:
        from codeprobe.core.llm import LLMParseError

        with (
            patch(
                "codeprobe.assess.heuristics.gather_heuristics", return_value=_RICH_REPO
            ),
            patch("codeprobe.core.llm.claude_available", return_value=True),
            patch(
                "codeprobe.assess.heuristics.score_repo_with_model",
                side_effect=LLMParseError("bad json"),
            ),
        ):
            result = assess_repo(Path("/any/path"))

        assert result.scoring_method == "heuristic"


class TestFallbackOnExecutionError:
    """assess_repo falls back to heuristic when subprocess fails."""

    def test_fallback_on_timeout(self) -> None:
        from codeprobe.core.llm import LLMExecutionError

        fake_h = _make_heuristics(total_commits=100, merge_commits=10)
        with (
            patch("codeprobe.assess.heuristics.gather_heuristics", return_value=fake_h),
            patch("codeprobe.core.llm.claude_available", return_value=True),
            patch(
                "codeprobe.assess.heuristics.score_repo_with_model",
                side_effect=LLMExecutionError("timed out"),
            ),
        ):
            result = assess_repo(Path("/any/path"))

        assert result.scoring_method == "heuristic"


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
                return FakeResult(
                    "src/main.py\nsrc/utils.py\ntests/test_main.py\nREADME.md\n"
                )
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
    """Mock gather_heuristics and verify scoring pipeline works."""

    def test_assess_repo_pipeline(self) -> None:
        with (
            patch(
                "codeprobe.assess.heuristics.gather_heuristics", return_value=_RICH_REPO
            ) as mock_gh,
            patch("codeprobe.core.llm.claude_available", return_value=False),
        ):
            result = assess_repo(Path("/any/path"))
            mock_gh.assert_called_once_with(Path("/any/path"))

        assert isinstance(result, AssessmentScore)
        assert 0.0 <= result.overall <= 1.0
        assert result.recommendation
        assert len(result.dimensions) == len(RUBRIC_V1)


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestScoreRepoAlias:
    """score_repo is an alias for score_repo_heuristic."""

    def test_alias_works(self) -> None:
        from codeprobe.assess.heuristics import score_repo

        h = _make_heuristics(total_commits=100, merge_commits=10)
        result = score_repo(h)
        assert result.scoring_method == "heuristic"
        assert isinstance(result, AssessmentScore)


# ---------------------------------------------------------------------------
# _has_tests — nested test directory detection
# ---------------------------------------------------------------------------


class TestHasTestsNestedDirs:
    """_has_tests must detect test dirs nested inside packages (e.g. numpy)."""

    def test_top_level_tests_dir(self, tmp_path: Path) -> None:
        """Standard top-level tests/ directory is detected."""
        from codeprobe.assess.heuristics import _has_tests

        (tmp_path / "tests").mkdir()
        assert _has_tests(tmp_path) is True

    def test_nested_tests_dir_via_git_ls_files(self, tmp_path: Path) -> None:
        """Nested test dirs like numpy/_core/tests/ are detected via git."""
        from codeprobe.assess.heuristics import _has_tests

        # No top-level test dirs exist
        # Mock _run_git to simulate git ls-files returning nested test files
        def fake_run_git(args: list[str], cwd: Path) -> str:
            if args[0] == "ls-files" and any(
                "**/test" in a or "**/tests" in a for a in args
            ):
                return "numpy/_core/tests/test_numeric.py"
            if args[0] == "ls-files":
                return ""
            return ""

        with patch("codeprobe.assess.heuristics._run_git", side_effect=fake_run_git):
            assert _has_tests(tmp_path) is True

    def test_nested_test_files_via_recursive_glob(self, tmp_path: Path) -> None:
        """Test files like src/pkg/test_foo.py are detected via recursive glob."""
        from codeprobe.assess.heuristics import _has_tests

        def fake_run_git(args: list[str], cwd: Path) -> str:
            if args[0] == "ls-files" and any("**/test_*.py" in a for a in args):
                return "src/pkg/test_foo.py"
            if args[0] == "ls-files":
                return ""
            return ""

        with patch("codeprobe.assess.heuristics._run_git", side_effect=fake_run_git):
            assert _has_tests(tmp_path) is True

    def test_no_tests_at_all(self, tmp_path: Path) -> None:
        """Repos with no test dirs or files return False."""
        from codeprobe.assess.heuristics import _has_tests

        def fake_run_git(args: list[str], cwd: Path) -> str:
            return ""

        with patch("codeprobe.assess.heuristics._run_git", side_effect=fake_run_git):
            assert _has_tests(tmp_path) is False
