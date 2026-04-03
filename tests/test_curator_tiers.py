"""Tests for curator_tiers: tier classification and curation verification."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from codeprobe.core.llm import LLMError, LLMResponse
from codeprobe.mining.curator import CuratedFile
from codeprobe.mining.curator_tiers import classify_tiers, verify_curation
from codeprobe.mining.org_scale_families import TaskFamily

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAMILY = TaskFamily(
    name="test-family",
    description="Test family for unit tests.",
    glob_patterns=("**/*.py",),
    content_patterns=(r"import\s+foo",),
)

REPOS = [Path("/tmp/test-repo")]


def _make_file(
    path: str,
    sources: tuple[str, ...] = ("grep",),
    tier: str = "required",
) -> CuratedFile:
    return CuratedFile(path=path, tier=tier, sources=sources)


# ---------------------------------------------------------------------------
# classify_tiers — heuristic path
# ---------------------------------------------------------------------------


class TestClassifyHeuristic:
    """Tests for classify_tiers with use_llm=False."""

    def test_two_sources_required(self) -> None:
        files = [_make_file("a.py", sources=("grep", "ast"))]
        result = classify_tiers(files, FAMILY, REPOS, use_llm=False)
        assert result[0].tier == "required"

    def test_three_sources_required(self) -> None:
        files = [_make_file("a.py", sources=("grep", "ast", "llm"))]
        result = classify_tiers(files, FAMILY, REPOS, use_llm=False)
        assert result[0].tier == "required"

    def test_one_source_supplementary(self) -> None:
        files = [_make_file("a.py", sources=("grep",))]
        result = classify_tiers(files, FAMILY, REPOS, use_llm=False)
        assert result[0].tier == "supplementary"

    def test_zero_sources_context(self) -> None:
        files = [_make_file("a.py", sources=())]
        result = classify_tiers(files, FAMILY, REPOS, use_llm=False)
        assert result[0].tier == "context"

    def test_mixed_sources(self) -> None:
        files = [
            _make_file("a.py", sources=("grep", "ast")),
            _make_file("b.py", sources=("grep",)),
            _make_file("c.py", sources=()),
        ]
        result = classify_tiers(files, FAMILY, REPOS, use_llm=False)
        assert result[0].tier == "required"
        assert result[1].tier == "supplementary"
        assert result[2].tier == "context"

    def test_empty_files(self) -> None:
        result = classify_tiers([], FAMILY, REPOS, use_llm=False)
        assert result == []

    def test_immutability(self) -> None:
        """Original CuratedFile objects must not be mutated."""
        original = _make_file("a.py", sources=("grep",), tier="required")
        result = classify_tiers([original], FAMILY, REPOS, use_llm=False)
        assert original.tier == "required"
        assert result[0].tier == "supplementary"
        assert result[0] is not original


# ---------------------------------------------------------------------------
# classify_tiers — LLM path
# ---------------------------------------------------------------------------


class TestClassifyLLM:
    """Tests for classify_tiers with use_llm=True (mocked)."""

    @patch("codeprobe.mining.curator_tiers.call_claude")
    def test_llm_classification(self, mock_call: object) -> None:
        response_json = json.dumps(
            {
                "src/core.py": "required",
                "src/util.py": "supplementary",
                "src/readme.py": "context",
            }
        )
        mock_call.return_value = LLMResponse(text=response_json)

        files = [
            _make_file("src/core.py"),
            _make_file("src/util.py"),
            _make_file("src/readme.py"),
        ]
        result = classify_tiers(files, FAMILY, REPOS, use_llm=True)

        assert result[0].tier == "required"
        assert result[1].tier == "supplementary"
        assert result[2].tier == "context"

        # Verify Haiku was called with correct params
        call_args = mock_call.call_args[0][0]
        assert call_args.model == "haiku"
        assert call_args.timeout_seconds == 30

    @patch("codeprobe.mining.curator_tiers.call_claude")
    def test_llm_with_markdown_fences(self, mock_call: object) -> None:
        response_text = '```json\n{"a.py": "required"}\n```'
        mock_call.return_value = LLMResponse(text=response_text)

        files = [_make_file("a.py")]
        result = classify_tiers(files, FAMILY, REPOS, use_llm=True)
        assert result[0].tier == "required"

    @patch("codeprobe.mining.curator_tiers.call_claude")
    def test_llm_error_falls_back_to_heuristic(self, mock_call: object) -> None:
        mock_call.side_effect = LLMError("API down")

        files = [
            _make_file("a.py", sources=("grep", "ast")),
            _make_file("b.py", sources=("grep",)),
        ]
        result = classify_tiers(files, FAMILY, REPOS, use_llm=True)
        assert result[0].tier == "required"
        assert result[1].tier == "supplementary"

    @patch("codeprobe.mining.curator_tiers.call_claude")
    def test_llm_invalid_json_falls_back(self, mock_call: object) -> None:
        mock_call.return_value = LLMResponse(text="not json at all")

        files = [_make_file("a.py", sources=("grep",))]
        result = classify_tiers(files, FAMILY, REPOS, use_llm=True)
        assert result[0].tier == "supplementary"

    @patch("codeprobe.mining.curator_tiers.call_claude")
    def test_llm_unmapped_file_uses_heuristic(self, mock_call: object) -> None:
        """If LLM response omits a file, heuristic is used for that file."""
        response_json = json.dumps({"a.py": "context"})
        mock_call.return_value = LLMResponse(text=response_json)

        files = [
            _make_file("a.py", sources=("grep", "ast")),
            _make_file("b.py", sources=("grep",)),
        ]
        result = classify_tiers(files, FAMILY, REPOS, use_llm=True)
        assert result[0].tier == "context"  # from LLM
        assert result[1].tier == "supplementary"  # heuristic fallback

    @patch("codeprobe.mining.curator_tiers.call_claude")
    def test_prompt_contains_temperature_instruction(self, mock_call: object) -> None:
        mock_call.return_value = LLMResponse(text='{"a.py": "required"}')

        files = [_make_file("a.py")]
        classify_tiers(files, FAMILY, REPOS, use_llm=True)

        prompt = mock_call.call_args[0][0].prompt
        assert "temperature=0" in prompt


# ---------------------------------------------------------------------------
# verify_curation
# ---------------------------------------------------------------------------


class TestVerifyCuration:
    """Tests for verify_curation."""

    @patch("codeprobe.mining.curator_tiers.get_tracked_files")
    @patch("codeprobe.mining.curator_tiers.call_claude")
    @patch("codeprobe.mining.curator_tiers.llm_available", return_value=True)
    def test_pass_zero_disagreements(
        self, mock_avail: object, mock_call: object, mock_tracked: object
    ) -> None:
        mock_tracked.return_value = frozenset({"a.py", "b.py", "x.py", "y.py"})
        response = json.dumps(
            {
                "a.py": "agree",
                "b.py": "agree",
                "x.py": "agree",
                "y.py": "agree",
            }
        )
        mock_call.return_value = LLMResponse(text=response)

        files = [_make_file("a.py"), _make_file("b.py")]
        result = verify_curation(files, FAMILY, REPOS, sample_size=5)
        assert result == "pass"

    @patch("codeprobe.mining.curator_tiers.get_tracked_files")
    @patch("codeprobe.mining.curator_tiers.call_claude")
    @patch("codeprobe.mining.curator_tiers.llm_available", return_value=True)
    def test_pass_one_disagreement(
        self, mock_avail: object, mock_call: object, mock_tracked: object
    ) -> None:
        mock_tracked.return_value = frozenset({"a.py", "x.py"})
        response = json.dumps({"a.py": "agree", "x.py": "disagree"})
        mock_call.return_value = LLMResponse(text=response)

        files = [_make_file("a.py")]
        result = verify_curation(files, FAMILY, REPOS, sample_size=5)
        assert result == "pass"

    @patch("codeprobe.mining.curator_tiers.get_tracked_files")
    @patch("codeprobe.mining.curator_tiers.call_claude")
    @patch("codeprobe.mining.curator_tiers.llm_available", return_value=True)
    def test_warn_two_disagreements(
        self, mock_avail: object, mock_call: object, mock_tracked: object
    ) -> None:
        mock_tracked.return_value = frozenset({"a.py", "b.py", "x.py", "y.py"})
        response = json.dumps(
            {
                "a.py": "disagree",
                "b.py": "disagree",
                "x.py": "agree",
                "y.py": "agree",
            }
        )
        mock_call.return_value = LLMResponse(text=response)

        files = [_make_file("a.py"), _make_file("b.py")]
        result = verify_curation(files, FAMILY, REPOS, sample_size=5)
        assert result == "warn"

    @patch("codeprobe.mining.curator_tiers.get_tracked_files")
    @patch("codeprobe.mining.curator_tiers.call_claude")
    @patch("codeprobe.mining.curator_tiers.llm_available", return_value=True)
    def test_fail_three_disagreements(
        self, mock_avail: object, mock_call: object, mock_tracked: object
    ) -> None:
        mock_tracked.return_value = frozenset(
            {"a.py", "b.py", "c.py", "x.py", "y.py", "z.py"}
        )
        response = json.dumps(
            {
                "a.py": "disagree",
                "b.py": "disagree",
                "c.py": "disagree",
                "x.py": "agree",
            }
        )
        mock_call.return_value = LLMResponse(text=response)

        files = [_make_file("a.py"), _make_file("b.py"), _make_file("c.py")]
        result = verify_curation(files, FAMILY, REPOS, sample_size=5)
        assert result == "fail"

    @patch("codeprobe.mining.curator_tiers.llm_available", return_value=False)
    def test_llm_unavailable_returns_pass(self, mock_avail: object) -> None:
        files = [_make_file("a.py")]
        result = verify_curation(files, FAMILY, REPOS, sample_size=5)
        assert result == "pass"

    @patch("codeprobe.mining.curator_tiers.get_tracked_files")
    @patch("codeprobe.mining.curator_tiers.call_claude")
    @patch("codeprobe.mining.curator_tiers.llm_available", return_value=True)
    def test_llm_call_error_returns_pass(
        self, mock_avail: object, mock_call: object, mock_tracked: object
    ) -> None:
        mock_tracked.return_value = frozenset({"a.py", "x.py"})
        mock_call.side_effect = LLMError("timeout")

        files = [_make_file("a.py")]
        result = verify_curation(files, FAMILY, REPOS, sample_size=5)
        assert result == "pass"

    @patch("codeprobe.mining.curator_tiers.get_tracked_files")
    @patch("codeprobe.mining.curator_tiers.call_claude")
    @patch("codeprobe.mining.curator_tiers.llm_available", return_value=True)
    def test_verify_calls_haiku(
        self, mock_avail: object, mock_call: object, mock_tracked: object
    ) -> None:
        mock_tracked.return_value = frozenset({"a.py", "x.py"})
        mock_call.return_value = LLMResponse(
            text=json.dumps({"a.py": "agree", "x.py": "agree"})
        )

        files = [_make_file("a.py")]
        verify_curation(files, FAMILY, REPOS, sample_size=5)

        call_args = mock_call.call_args[0][0]
        assert call_args.model == "haiku"
        assert call_args.timeout_seconds == 30
