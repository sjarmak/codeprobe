"""Tests for Sourcegraph ground truth enrichment."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from codeprobe.mining.sg_ground_truth import enrich_ground_truth

# ---------------------------------------------------------------------------
# Mock SG API response helpers
# ---------------------------------------------------------------------------


def _sg_response(file_paths: list[str]) -> dict:
    """Build a mock JSON-RPC response from Sourcegraph find_references."""
    locations = [
        {"uri": f"file:///{fp}", "range": {"start": {"line": 1, "character": 0}}}
        for fp in file_paths
    ]
    return {"result": locations}


# ---------------------------------------------------------------------------
# Tier assignment logic
# ---------------------------------------------------------------------------


class TestTierAssignment:
    """Verify required vs supplementary tier classification."""

    def test_both_grep_and_sg_are_required(self) -> None:
        grep_files = frozenset({"a.py", "b.py"})
        sg_files = ["a.py", "b.py", "c.py"]

        with patch("codeprobe.mining.sg_ground_truth.requests") as mock_req:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = _sg_response(sg_files)
            mock_req.post.return_value = mock_resp

            all_files, tier_map = enrich_ground_truth(
                symbol="foo",
                defining_file="foo.py",
                grep_files=grep_files,
                repo_sg_name="github.com/sg-evals/myrepo",
                sg_token="test-token",
            )

        # a.py and b.py found by both -> required
        assert tier_map["a.py"] == "required"
        assert tier_map["b.py"] == "required"
        # c.py found only by SG -> supplementary
        assert tier_map["c.py"] == "supplementary"
        # All files present in the union
        assert all_files == frozenset({"a.py", "b.py", "c.py"})

    def test_grep_only_files_are_required(self) -> None:
        grep_files = frozenset({"a.py", "b.py"})
        sg_files = ["b.py"]  # SG only finds b

        with patch("codeprobe.mining.sg_ground_truth.requests") as mock_req:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = _sg_response(sg_files)
            mock_req.post.return_value = mock_resp

            all_files, tier_map = enrich_ground_truth(
                symbol="foo",
                defining_file="foo.py",
                grep_files=grep_files,
                repo_sg_name="github.com/sg-evals/myrepo",
                sg_token="test-token",
            )

        # a.py found only by grep -> required
        assert tier_map["a.py"] == "required"
        # b.py found by both -> required
        assert tier_map["b.py"] == "required"
        assert all_files == frozenset({"a.py", "b.py"})


class TestAPICall:
    """Verify the HTTP call shape and error handling."""

    def test_authorization_header_sent(self) -> None:
        with patch("codeprobe.mining.sg_ground_truth.requests") as mock_req:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = _sg_response(["x.py"])
            mock_req.post.return_value = mock_resp

            enrich_ground_truth(
                symbol="bar",
                defining_file="bar.py",
                grep_files=frozenset({"bar.py"}),
                repo_sg_name="github.com/sg-evals/repo",
                sg_token="secret-tok-123",
                sg_url="https://custom.sg.com",
            )

            call_args = mock_req.post.call_args
            headers = call_args.kwargs.get("headers", call_args[1].get("headers", {}))
            assert headers["Authorization"] == "token secret-tok-123"

    def test_custom_sg_url(self) -> None:
        with patch("codeprobe.mining.sg_ground_truth.requests") as mock_req:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = _sg_response([])
            mock_req.post.return_value = mock_resp

            enrich_ground_truth(
                symbol="baz",
                defining_file="baz.py",
                grep_files=frozenset(),
                repo_sg_name="github.com/sg-evals/repo",
                sg_token="tok",
                sg_url="https://my-sg.example.com",
            )

            url_called = mock_req.post.call_args[0][0]
            assert url_called == "https://my-sg.example.com/.api/mcp/v1"


class TestGracefulDegradation:
    """Verify fallback when SG API fails."""

    def test_http_error_returns_grep_only(self) -> None:
        grep_files = frozenset({"a.py", "b.py"})

        with patch("codeprobe.mining.sg_ground_truth.requests") as mock_req:
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            mock_resp.raise_for_status.side_effect = Exception("Server error")
            mock_req.post.return_value = mock_resp
            mock_req.exceptions = type("E", (), {"RequestException": Exception})

            all_files, tier_map = enrich_ground_truth(
                symbol="foo",
                defining_file="foo.py",
                grep_files=grep_files,
                repo_sg_name="github.com/sg-evals/myrepo",
                sg_token="test-token",
            )

        # Falls back to grep files, all required
        assert all_files == grep_files
        assert all(v == "required" for v in tier_map.values())

    def test_timeout_returns_grep_only(self) -> None:
        grep_files = frozenset({"a.py"})

        with patch("codeprobe.mining.sg_ground_truth.requests") as mock_req:
            mock_req.post.side_effect = Exception("Connection timed out")
            mock_req.exceptions = type("E", (), {"RequestException": Exception})

            all_files, tier_map = enrich_ground_truth(
                symbol="foo",
                defining_file="foo.py",
                grep_files=grep_files,
                repo_sg_name="github.com/sg-evals/myrepo",
                sg_token="test-token",
            )

        assert all_files == grep_files

    def test_malformed_response_returns_grep_only(self) -> None:
        grep_files = frozenset({"a.py"})

        with patch("codeprobe.mining.sg_ground_truth.requests") as mock_req:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"unexpected": "shape"}
            mock_req.post.return_value = mock_resp

            all_files, tier_map = enrich_ground_truth(
                symbol="foo",
                defining_file="foo.py",
                grep_files=grep_files,
                repo_sg_name="github.com/sg-evals/myrepo",
                sg_token="test-token",
            )

        assert all_files == grep_files


class TestPathExtraction:
    """Verify file path normalization from SG URIs."""

    def test_strips_repo_prefix_from_paths(self) -> None:
        """SG returns absolute repo paths; we need repo-relative paths."""
        sg_files = ["numpy/_core/fromnumeric.py", "numpy/ma/core.py"]

        with patch("codeprobe.mining.sg_ground_truth.requests") as mock_req:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = _sg_response(sg_files)
            mock_req.post.return_value = mock_resp

            all_files, tier_map = enrich_ground_truth(
                symbol="amax",
                defining_file="numpy/_core/fromnumeric.py",
                grep_files=frozenset(),
                repo_sg_name="github.com/sg-evals/numpy",
                sg_token="tok",
            )

        assert "numpy/_core/fromnumeric.py" in all_files
        assert "numpy/ma/core.py" in all_files


class TestTokenNotLogged:
    """Ensure the token value never appears in log output."""

    def test_token_not_in_exception_message(self) -> None:
        import logging

        with patch("codeprobe.mining.sg_ground_truth.requests") as mock_req:
            mock_req.post.side_effect = Exception("Connection refused")
            mock_req.exceptions = type("E", (), {"RequestException": Exception})

            with patch("codeprobe.mining.sg_ground_truth.logger") as mock_logger:
                enrich_ground_truth(
                    symbol="foo",
                    defining_file="foo.py",
                    grep_files=frozenset({"a.py"}),
                    repo_sg_name="github.com/sg-evals/myrepo",
                    sg_token="super-secret-token-value",
                )

                # Check all warning calls don't contain the token
                for call in mock_logger.warning.call_args_list:
                    msg = str(call)
                    assert "super-secret-token-value" not in msg
