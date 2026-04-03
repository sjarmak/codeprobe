"""Tests for curator_backends: GrepBackend, SourcegraphBackend, PRDiffBackend, AgentSearchBackend."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codeprobe.core.llm import LLMError, LLMResponse
from codeprobe.mining.curator import CuratedFile, CurationBackend
from codeprobe.mining.curator_backends import (
    AgentSearchBackend,
    GrepBackend,
    PRDiffBackend,
    SourcegraphBackend,
    _MAX_FILE_LISTING,
)
from codeprobe.mining.org_scale_families import TaskFamily
from codeprobe.mining.org_scale_scanner import FamilyScanResult, PatternHit

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_family() -> TaskFamily:
    return TaskFamily(
        name="test-family",
        description="Test family for unit tests",
        glob_patterns=("**/*.py", "**/*.go"),
        content_patterns=(r"@Deprecated", r"warnings\.warn"),
        min_hits=1,
        max_hits=100,
    )


@pytest.fixture()
def sample_repos(tmp_path: Path) -> list[Path]:
    return [tmp_path / "repo1"]


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_grep_backend_is_curation_backend(self) -> None:
        assert isinstance(GrepBackend(), CurationBackend)

    def test_sourcegraph_backend_is_curation_backend(self) -> None:
        assert isinstance(SourcegraphBackend(), CurationBackend)

    def test_pr_diff_backend_is_curation_backend(self) -> None:
        assert isinstance(PRDiffBackend(), CurationBackend)

    def test_agent_search_backend_is_curation_backend(self) -> None:
        assert isinstance(AgentSearchBackend(), CurationBackend)


# ---------------------------------------------------------------------------
# GrepBackend
# ---------------------------------------------------------------------------


class TestGrepBackend:
    def test_name(self) -> None:
        assert GrepBackend().name == "grep"

    def test_available_always_true(self) -> None:
        assert GrepBackend().available() is True

    def test_search_converts_scan_result(
        self, sample_family: TaskFamily, sample_repos: list[Path]
    ) -> None:
        hits = (
            PatternHit("src/a.py", 10, "@Deprecated", r"@Deprecated"),
            PatternHit("src/a.py", 20, "@Deprecated", r"@Deprecated"),
            PatternHit("src/b.py", 5, "warnings.warn", r"warnings\.warn"),
        )
        scan_result = FamilyScanResult(
            family=sample_family,
            hits=hits,
            repo_paths=(sample_repos[0],),
            commit_sha="abc123",
            matched_files=frozenset({"src/a.py", "src/b.py"}),
        )

        with patch(
            "codeprobe.mining.curator_backends.scan_repo_for_family",
            return_value=scan_result,
        ):
            backend = GrepBackend()
            result = backend.search(sample_repos, sample_family)

        assert len(result) == 2
        # Results sorted by path
        a_file = result[0]
        assert a_file.path == "src/a.py"
        assert a_file.sources == ("grep",)
        assert a_file.confidence == 1.0
        assert a_file.hit_count == 2
        assert a_file.line_matches == (10, 20)

        b_file = result[1]
        assert b_file.path == "src/b.py"
        assert b_file.hit_count == 1

    def test_search_empty_result(
        self, sample_family: TaskFamily, sample_repos: list[Path]
    ) -> None:
        scan_result = FamilyScanResult(
            family=sample_family,
            hits=(),
            repo_paths=(sample_repos[0],),
            commit_sha="abc123",
            matched_files=frozenset(),
        )
        with patch(
            "codeprobe.mining.curator_backends.scan_repo_for_family",
            return_value=scan_result,
        ):
            result = GrepBackend().search(sample_repos, sample_family)

        assert result == []


# ---------------------------------------------------------------------------
# SourcegraphBackend
# ---------------------------------------------------------------------------


class TestSourcegraphBackend:
    def test_name(self) -> None:
        assert SourcegraphBackend().name == "sourcegraph"

    def test_available_with_token(self) -> None:
        with patch.dict("os.environ", {"SOURCEGRAPH_TOKEN": "tok123"}):
            assert SourcegraphBackend().available() is True

    def test_available_without_token(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            assert SourcegraphBackend().available() is False

    def test_search_parses_graphql_response(
        self, sample_family: TaskFamily, sample_repos: list[Path]
    ) -> None:
        api_response = {
            "data": {
                "search": {
                    "results": {
                        "results": [
                            {"file": {"path": "src/a.py"}},
                            {"file": {"path": "src/b.py"}},
                        ]
                    }
                }
            }
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(api_response).encode("utf-8")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with (
            patch.dict(
                "os.environ",
                {
                    "SOURCEGRAPH_TOKEN": "tok",
                    "SOURCEGRAPH_ENDPOINT": "https://sg.example.com/.api/graphql",
                },
            ),
            patch("urllib.request.urlopen", return_value=mock_resp),
        ):
            backend = SourcegraphBackend()
            result = backend.search(sample_repos, sample_family)

        # Should have files from the response (deduplicated across patterns)
        paths = {cf.path for cf in result}
        assert "src/a.py" in paths
        assert "src/b.py" in paths
        for cf in result:
            assert cf.sources == ("sourcegraph",)
            assert cf.confidence == 0.9

    def test_search_retries_on_429(
        self, sample_family: TaskFamily, sample_repos: list[Path]
    ) -> None:
        """Verify retry with backoff on HTTP 429."""
        # First call: 429, second call: success
        api_response = {
            "data": {"search": {"results": {"results": [{"file": {"path": "x.py"}}]}}}
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(api_response).encode("utf-8")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        http_429 = urllib.error.HTTPError(
            "https://sg.example.com", 429, "Too Many Requests", {}, None
        )

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                raise http_429
            return mock_resp

        with (
            patch.dict(
                "os.environ",
                {
                    "SOURCEGRAPH_TOKEN": "tok",
                    "SOURCEGRAPH_ENDPOINT": "https://sg.example.com/.api/graphql",
                },
            ),
            patch("urllib.request.urlopen", side_effect=side_effect),
            patch("time.sleep") as mock_sleep,
        ):
            backend = SourcegraphBackend()
            # Use a family with only 1 content pattern to simplify
            single_pattern_family = TaskFamily(
                name="test",
                description="test",
                glob_patterns=("**/*.py",),
                content_patterns=(r"@Deprecated",),
            )
            result = backend.search(sample_repos, single_pattern_family)

        assert len(result) == 1
        assert result[0].path == "x.py"
        mock_sleep.assert_called_once()

    def test_search_missing_env(
        self, sample_family: TaskFamily, sample_repos: list[Path]
    ) -> None:
        with patch.dict("os.environ", {}, clear=True):
            result = SourcegraphBackend().search(sample_repos, sample_family)
        assert result == []


# ---------------------------------------------------------------------------
# PRDiffBackend
# ---------------------------------------------------------------------------


class TestPRDiffBackend:
    def test_name(self) -> None:
        assert PRDiffBackend().name == "pr_diff"

    def test_available_always_true(self) -> None:
        assert PRDiffBackend().available() is True

    def test_search_filters_by_glob(
        self, sample_family: TaskFamily, sample_repos: list[Path]
    ) -> None:
        git_output = "src/main.py\nsrc/util.go\nREADME.md\ndata.csv\n"
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = git_output

        with (
            patch("subprocess.run", return_value=mock_result),
            patch(
                "codeprobe.mining.curator_backends._file_matches_content",
                return_value=True,
            ),
        ):
            backend = PRDiffBackend()
            result = backend.search(sample_repos, sample_family)

        paths = [cf.path for cf in result]
        assert "src/main.py" in paths
        assert "src/util.go" in paths
        assert "README.md" not in paths
        assert "data.csv" not in paths
        for cf in result:
            assert cf.sources == ("pr_diff",)
            assert cf.confidence == 0.7

    def test_search_git_failure(
        self, sample_family: TaskFamily, sample_repos: list[Path]
    ) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        with patch("subprocess.run", return_value=mock_result):
            result = PRDiffBackend().search(sample_repos, sample_family)

        assert result == []

    def test_search_deduplicates(
        self, sample_family: TaskFamily, tmp_path: Path
    ) -> None:
        """Files appearing in multiple repos should be deduplicated."""
        repos = [tmp_path / "r1", tmp_path / "r2"]
        git_output = "src/shared.py\n"
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = git_output

        with (
            patch("subprocess.run", return_value=mock_result),
            patch(
                "codeprobe.mining.curator_backends._file_matches_content",
                return_value=True,
            ),
        ):
            result = PRDiffBackend().search(repos, sample_family)

        paths = [cf.path for cf in result]
        assert paths.count("src/shared.py") == 1


# ---------------------------------------------------------------------------
# AgentSearchBackend
# ---------------------------------------------------------------------------


class TestAgentSearchBackend:
    def test_name(self) -> None:
        assert AgentSearchBackend().name == "agent_search"

    def test_available_delegates_to_llm_available(self) -> None:
        with patch(
            "codeprobe.mining.curator_backends.llm_available", return_value=True
        ):
            assert AgentSearchBackend().available() is True
        with patch(
            "codeprobe.mining.curator_backends.llm_available", return_value=False
        ):
            assert AgentSearchBackend().available() is False

    def test_search_parses_llm_response(
        self, sample_family: TaskFamily, sample_repos: list[Path]
    ) -> None:
        tracked = frozenset({"src/a.py", "src/b.py", "src/c.go", "README.md"})
        llm_response = LLMResponse(
            text='["src/a.py", "src/c.go"]',
            backend="anthropic",
        )

        with (
            patch(
                "codeprobe.mining.org_scale_scanner.get_tracked_files",
                return_value=tracked,
            ),
            patch(
                "codeprobe.mining.curator_backends.call_claude",
                return_value=llm_response,
            ),
        ):
            result = AgentSearchBackend().search(sample_repos, sample_family)

        paths = [cf.path for cf in result]
        assert "src/a.py" in paths
        assert "src/c.go" in paths
        assert "README.md" not in paths
        for cf in result:
            assert cf.sources == ("agent_search",)
            assert cf.confidence == 0.6

    def test_search_caps_at_2000_files(
        self, sample_family: TaskFamily, sample_repos: list[Path]
    ) -> None:
        # Create more than 2000 tracked files
        tracked = frozenset({f"src/file_{i}.py" for i in range(3000)})
        llm_response = LLMResponse(text="[]", backend="anthropic")

        captured_prompt = {}

        def capture_call(req):
            captured_prompt["prompt"] = req.prompt
            return llm_response

        with (
            patch(
                "codeprobe.mining.org_scale_scanner.get_tracked_files",
                return_value=tracked,
            ),
            patch(
                "codeprobe.mining.curator_backends.call_claude",
                side_effect=capture_call,
            ),
        ):
            AgentSearchBackend().search(sample_repos, sample_family)

        # Verify prompt mentions capped count
        assert f"({_MAX_FILE_LISTING} files)" in captured_prompt["prompt"]

    def test_search_handles_llm_error(
        self, sample_family: TaskFamily, sample_repos: list[Path]
    ) -> None:
        tracked = frozenset({"src/a.py"})

        with (
            patch(
                "codeprobe.mining.org_scale_scanner.get_tracked_files",
                return_value=tracked,
            ),
            patch(
                "codeprobe.mining.curator_backends.call_claude",
                side_effect=LLMError("timeout"),
            ),
        ):
            result = AgentSearchBackend().search(sample_repos, sample_family)

        assert result == []

    def test_search_ignores_invalid_files_from_llm(
        self, sample_family: TaskFamily, sample_repos: list[Path]
    ) -> None:
        tracked = frozenset({"src/a.py"})
        llm_response = LLMResponse(
            text='["src/a.py", "nonexistent.py"]',
            backend="anthropic",
        )

        with (
            patch(
                "codeprobe.mining.org_scale_scanner.get_tracked_files",
                return_value=tracked,
            ),
            patch(
                "codeprobe.mining.curator_backends.call_claude",
                return_value=llm_response,
            ),
        ):
            result = AgentSearchBackend().search(sample_repos, sample_family)

        assert len(result) == 1
        assert result[0].path == "src/a.py"

    def test_search_handles_malformed_json(
        self, sample_family: TaskFamily, sample_repos: list[Path]
    ) -> None:
        tracked = frozenset({"src/a.py"})
        llm_response = LLMResponse(text="not valid json", backend="anthropic")

        with (
            patch(
                "codeprobe.mining.org_scale_scanner.get_tracked_files",
                return_value=tracked,
            ),
            patch(
                "codeprobe.mining.curator_backends.call_claude",
                return_value=llm_response,
            ),
        ):
            result = AgentSearchBackend().search(sample_repos, sample_family)

        assert result == []

    def test_search_empty_repo(
        self, sample_family: TaskFamily, sample_repos: list[Path]
    ) -> None:
        with patch(
            "codeprobe.mining.org_scale_scanner.get_tracked_files",
            return_value=frozenset(),
        ):
            result = AgentSearchBackend().search(sample_repos, sample_family)

        assert result == []
