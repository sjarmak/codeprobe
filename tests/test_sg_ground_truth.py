"""Tests for Sourcegraph ground truth enrichment."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from codeprobe.mining.sg_auth import CachedToken
from codeprobe.mining.sg_ground_truth import enrich_ground_truth

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FAKE_TOKEN = CachedToken(
    access_token="test-token",
    refresh_token=None,
    expires_at=None,
    endpoint="https://demo.sourcegraph.com",
)


@pytest.fixture(autouse=True)
def _mock_auth() -> None:
    """Patch get_valid_token so tests never hit real auth."""
    with patch(
        "codeprobe.mining.sg_auth.get_valid_token",
        return_value=_FAKE_TOKEN,
    ):
        yield


# ---------------------------------------------------------------------------
# Mock SG SSE response helpers
# ---------------------------------------------------------------------------


def _sg_sse_response(file_paths: list[str], repo: str = "github.com/sg-evals/myrepo"):
    """Build a mock SSE streaming response from sg_find_references.

    The real SG MCP endpoint returns text/event-stream with a single
    ``data:`` line containing a JSON-RPC result whose ``content`` list
    has one text item with header lines like ``# repo → file``.
    """
    text_lines = []
    for fp in file_paths:
        text_lines.append(f"# {repo} \u2013 {fp}")
        text_lines.append("1: some code here")
        text_lines.append("")

    content_text = "\n".join(text_lines)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": content_text}],
        },
    }
    sse_line = f"data: {json.dumps(payload)}"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status.return_value = None
    mock_resp.iter_lines.return_value = iter([sse_line])
    return mock_resp


def _sg_empty_response():
    """SSE response with empty content (symbol not found)."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": []},
    }
    sse_line = f"data: {json.dumps(payload)}"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status.return_value = None
    mock_resp.iter_lines.return_value = iter([sse_line])
    return mock_resp


def _sg_error_response():
    """SSE response with JSON-RPC error."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32602, "message": "invalid params"},
    }
    sse_line = f"data: {json.dumps(payload)}"

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status.return_value = None
    mock_resp.iter_lines.return_value = iter([sse_line])
    return mock_resp


# ---------------------------------------------------------------------------
# Tier assignment logic
# ---------------------------------------------------------------------------


class TestTierAssignment:
    """Verify required vs supplementary tier classification."""

    def test_both_grep_and_sg_are_required(self) -> None:
        grep_files = frozenset({"a.py", "b.py"})
        sg_files = ["a.py", "b.py", "c.py"]
        repo = "github.com/sg-evals/myrepo"

        with patch("codeprobe.mining.sg_ground_truth.requests") as mock_req:
            mock_req.post.return_value = _sg_sse_response(sg_files, repo=repo)

            all_files, tier_map = enrich_ground_truth(
                symbol="foo",
                defining_file="foo.py",
                grep_files=grep_files,
                repo_sg_name=repo,
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
        repo = "github.com/sg-evals/myrepo"

        with patch("codeprobe.mining.sg_ground_truth.requests") as mock_req:
            mock_req.post.return_value = _sg_sse_response(sg_files, repo=repo)

            all_files, tier_map = enrich_ground_truth(
                symbol="foo",
                defining_file="foo.py",
                grep_files=grep_files,
                repo_sg_name=repo,
            )

        # a.py found only by grep -> required
        assert tier_map["a.py"] == "required"
        # b.py found by both -> required
        assert tier_map["b.py"] == "required"
        assert all_files == frozenset({"a.py", "b.py"})


class TestAPICall:
    """Verify the HTTP call shape and error handling."""

    def test_authorization_header_sent(self) -> None:
        repo = "github.com/sg-evals/repo"

        with patch("codeprobe.mining.sg_ground_truth.requests") as mock_req:
            mock_req.post.return_value = _sg_sse_response(["x.py"], repo=repo)

            enrich_ground_truth(
                symbol="bar",
                defining_file="bar.py",
                grep_files=frozenset({"bar.py"}),
                repo_sg_name=repo,
                sg_url="https://custom.sg.com",
            )

            call_args = mock_req.post.call_args
            headers = call_args.kwargs.get("headers", call_args[1].get("headers", {}))
            assert headers["Authorization"] == "token test-token"

    def test_custom_sg_url(self) -> None:
        with patch("codeprobe.mining.sg_ground_truth.requests") as mock_req:
            mock_req.post.return_value = _sg_empty_response()

            enrich_ground_truth(
                symbol="baz",
                defining_file="baz.py",
                grep_files=frozenset(),
                repo_sg_name="github.com/sg-evals/repo",
                sg_url="https://my-sg.example.com",
            )

            url_called = mock_req.post.call_args[0][0]
            assert url_called == "https://my-sg.example.com/.api/mcp/v1"

    def test_tools_call_method_and_params(self) -> None:
        """Verify we send tools/call with sg_find_references params."""
        repo = "github.com/sg-evals/repo"

        with patch("codeprobe.mining.sg_ground_truth.requests") as mock_req:
            mock_req.post.return_value = _sg_empty_response()

            enrich_ground_truth(
                symbol="MyClass",
                defining_file="src/models.py",
                grep_files=frozenset(),
                repo_sg_name=repo,
            )

            call_args = mock_req.post.call_args
            payload = call_args.kwargs.get("json", call_args[1].get("json", {}))
            assert payload["method"] == "tools/call"
            params = payload["params"]
            assert params["name"] == "sg_find_references"
            assert params["arguments"]["repo"] == repo
            assert params["arguments"]["path"] == "src/models.py"
            assert params["arguments"]["symbol"] == "MyClass"


class TestRetryOn401:
    """Verify 401 triggers a token refresh and retry."""

    def test_401_triggers_refresh_and_retry(self) -> None:
        repo = "github.com/sg-evals/repo"
        call_count = 0

        refreshed_token = CachedToken(
            access_token="refreshed-token",
            refresh_token=None,
            expires_at=None,
            endpoint="https://demo.sourcegraph.com",
        )

        def mock_get_valid_token(*args, **kwargs):
            if kwargs.get("force_refresh"):
                return refreshed_token
            return _FAKE_TOKEN

        resp_401 = MagicMock()
        resp_401.status_code = 401

        resp_ok = _sg_sse_response(["x.py"], repo=repo)

        with (
            patch("codeprobe.mining.sg_ground_truth.requests") as mock_req,
            patch(
                "codeprobe.mining.sg_auth.get_valid_token",
                side_effect=mock_get_valid_token,
            ),
        ):
            mock_req.post.side_effect = [resp_401, resp_ok]
            mock_req.HTTPError = Exception

            all_files, tier_map = enrich_ground_truth(
                symbol="foo",
                defining_file="foo.py",
                grep_files=frozenset(),
                repo_sg_name=repo,
            )

        assert "x.py" in all_files
        # Two calls: first with expired token, second with refreshed token
        assert mock_req.post.call_count == 2

    def test_401_after_refresh_returns_none(self) -> None:
        """If refresh also fails, degrade to grep-only."""
        from codeprobe.mining.sg_auth import AuthError

        grep_files = frozenset({"a.py"})
        repo = "github.com/sg-evals/repo"

        resp_401 = MagicMock()
        resp_401.status_code = 401

        def mock_get_valid_token(*args, **kwargs):
            if kwargs.get("force_refresh"):
                raise AuthError("Cannot refresh")
            return _FAKE_TOKEN

        with (
            patch("codeprobe.mining.sg_ground_truth.requests") as mock_req,
            patch(
                "codeprobe.mining.sg_auth.get_valid_token",
                side_effect=mock_get_valid_token,
            ),
        ):
            mock_req.post.return_value = resp_401
            mock_req.HTTPError = Exception

            all_files, tier_map = enrich_ground_truth(
                symbol="foo",
                defining_file="foo.py",
                grep_files=grep_files,
                repo_sg_name=repo,
            )

        # Falls back to grep-only
        assert all_files == grep_files


class TestGracefulDegradation:
    """Verify fallback when SG API fails."""

    def test_http_error_returns_grep_only(self) -> None:
        grep_files = frozenset({"a.py", "b.py"})

        with patch("codeprobe.mining.sg_ground_truth.requests") as mock_req:
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            mock_resp.raise_for_status.side_effect = Exception("Server error")
            mock_req.post.return_value = mock_resp
            mock_req.HTTPError = type("HTTPError", (Exception,), {})

            all_files, tier_map = enrich_ground_truth(
                symbol="foo",
                defining_file="foo.py",
                grep_files=grep_files,
                repo_sg_name="github.com/sg-evals/myrepo",
            )

        # Falls back to grep files, all required
        assert all_files == grep_files
        assert all(v == "required" for v in tier_map.values())

    def test_timeout_returns_grep_only(self) -> None:
        grep_files = frozenset({"a.py"})

        with patch("codeprobe.mining.sg_ground_truth.requests") as mock_req:
            mock_req.post.side_effect = Exception("Connection timed out")

            all_files, tier_map = enrich_ground_truth(
                symbol="foo",
                defining_file="foo.py",
                grep_files=grep_files,
                repo_sg_name="github.com/sg-evals/myrepo",
            )

        assert all_files == grep_files

    def test_malformed_response_returns_grep_only(self) -> None:
        """SSE response with no parseable file paths degrades gracefully."""
        grep_files = frozenset({"a.py"})

        with patch("codeprobe.mining.sg_ground_truth.requests") as mock_req:
            mock_req.post.return_value = _sg_empty_response()

            all_files, tier_map = enrich_ground_truth(
                symbol="foo",
                defining_file="foo.py",
                grep_files=grep_files,
                repo_sg_name="github.com/sg-evals/myrepo",
            )

        # SG returned empty, so only grep files remain (all required)
        assert all_files == grep_files

    def test_jsonrpc_error_returns_grep_only(self) -> None:
        """JSON-RPC error in SSE response degrades to grep-only."""
        grep_files = frozenset({"a.py"})

        with patch("codeprobe.mining.sg_ground_truth.requests") as mock_req:
            mock_req.post.return_value = _sg_error_response()

            all_files, tier_map = enrich_ground_truth(
                symbol="foo",
                defining_file="foo.py",
                grep_files=grep_files,
                repo_sg_name="github.com/sg-evals/myrepo",
            )

        assert all_files == grep_files

    def test_no_auth_returns_grep_only(self) -> None:
        """When no auth is available, degrade to grep-only."""
        from codeprobe.mining.sg_auth import AuthError

        grep_files = frozenset({"a.py"})

        with patch(
            "codeprobe.mining.sg_auth.get_valid_token",
            side_effect=AuthError("No auth"),
        ):
            all_files, tier_map = enrich_ground_truth(
                symbol="foo",
                defining_file="foo.py",
                grep_files=grep_files,
                repo_sg_name="github.com/sg-evals/myrepo",
            )

        assert all_files == grep_files


class TestPathExtraction:
    """Verify file path normalization from SG SSE content."""

    def test_extracts_paths_from_header_lines(self) -> None:
        """SG returns header lines like '# repo – file'; we extract the file."""
        repo = "github.com/sg-evals/numpy"
        sg_files = ["numpy/_core/fromnumeric.py", "numpy/ma/core.py"]

        with patch("codeprobe.mining.sg_ground_truth.requests") as mock_req:
            mock_req.post.return_value = _sg_sse_response(sg_files, repo=repo)

            all_files, tier_map = enrich_ground_truth(
                symbol="amax",
                defining_file="numpy/_core/fromnumeric.py",
                grep_files=frozenset(),
                repo_sg_name=repo,
            )

        assert "numpy/_core/fromnumeric.py" in all_files
        assert "numpy/ma/core.py" in all_files


class TestTokenNotLogged:
    """Ensure the token value never appears in log output."""

    def test_token_not_in_exception_message(self) -> None:
        with (
            patch("codeprobe.mining.sg_ground_truth.requests") as mock_req,
            patch("codeprobe.mining.sg_ground_truth.logger") as mock_logger,
        ):
            mock_req.post.side_effect = Exception("Connection refused")

            enrich_ground_truth(
                symbol="foo",
                defining_file="foo.py",
                grep_files=frozenset({"a.py"}),
                repo_sg_name="github.com/sg-evals/myrepo",
            )

            # Check all warning calls don't contain the token
            for call in mock_logger.warning.call_args_list:
                msg = str(call)
                assert "test-token" not in msg
