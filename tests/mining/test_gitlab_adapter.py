"""Tests for codeprobe.mining.vcs.gitlab.GitLabAdapter."""

from __future__ import annotations

from typing import Any

import pytest

from codeprobe.mining.vcs import gitlab as gitlab_module
from codeprobe.mining.vcs.base import AuthFailure, AuthMode, MergeRequest
from codeprobe.mining.vcs.gitlab import GitLabAdapter


class _HttpStub:
    """Records calls and returns a canned (status, body, headers) per URL."""

    def __init__(self, responses: list[tuple[int, Any, dict[str, str]]]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, url: str, headers: dict[str, str], timeout: float = 15.0
    ) -> tuple[int, Any, dict[str, str]]:
        self.calls.append({"url": url, "headers": dict(headers), "timeout": timeout})
        if not self._responses:
            return (200, {}, {})
        return self._responses.pop(0)


_MR_JSON = {
    "id": 101,
    "iid": 42,
    "title": "Fix widget alignment",
    "description": "Closes FOO-1. Aligns widget to grid.",
    "state": "merged",
    "source_branch": "feat/widget",
    "target_branch": "main",
    "merge_commit_sha": "abc123def",
    "web_url": "https://gitlab.com/acme/proj/-/merge_requests/42",
    "author": {"username": "alice"},
    "labels": ["bug", "frontend"],
}


@pytest.mark.parametrize(
    "mode,header_key,header_val_fmt",
    [
        (AuthMode.PAT, "PRIVATE-TOKEN", "{tok}"),
        (AuthMode.OAUTH2, "Authorization", "Bearer {tok}"),
    ],
)
def test_list_merges_sends_correct_auth_header(
    monkeypatch: pytest.MonkeyPatch,
    mode: AuthMode,
    header_key: str,
    header_val_fmt: str,
) -> None:
    stub = _HttpStub([(200, [_MR_JSON], {})])
    monkeypatch.setattr(gitlab_module, "_http_get", stub)

    adapter = GitLabAdapter("tok_abc", auth_mode=mode)
    results = list(adapter.list_merges("acme/proj", limit=5))

    assert len(results) == 1
    mr = results[0]
    assert isinstance(mr, MergeRequest)
    assert mr.iid == 42
    assert mr.title == "Fix widget alignment"
    assert mr.author == "alice"
    assert mr.labels == ("bug", "frontend")

    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert "merge_requests" in call["url"]
    assert "state=merged" in call["url"]
    assert call["headers"].get(header_key) == header_val_fmt.format(tok="tok_abc")


def test_pr_context_returns_rich_dict_with_enrichment_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commits_payload = [{"id": "sha1"}, {"id": "sha2"}]
    stub = _HttpStub(
        [
            (200, _MR_JSON, {}),
            (200, commits_payload, {}),
        ]
    )
    monkeypatch.setattr(gitlab_module, "_http_get", stub)

    adapter = GitLabAdapter("tok_abc", auth_mode=AuthMode.PAT)
    ctx = adapter.pr_context("acme/proj", 42)

    assert ctx["enrichment_source"] == "gitlab"
    assert ctx["title"] == "Fix widget alignment"
    assert ctx["description"].startswith("Closes FOO-1")
    assert ctx["author"] == "alice"
    assert ctx["labels"] == ("bug", "frontend")
    assert ctx["source_branch"] == "feat/widget"
    assert ctx["target_branch"] == "main"
    assert ctx["merge_commit_sha"] == "abc123def"
    assert ctx["commit_shas"] == ("sha1", "sha2")


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_raises_with_remediation_url(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    stub = _HttpStub([(status, {"message": "forbidden"}, {})])
    monkeypatch.setattr(gitlab_module, "_http_get", stub)

    adapter = GitLabAdapter("tok_abc", auth_mode=AuthMode.PAT)
    with pytest.raises(AuthFailure) as exc_info:
        list(adapter.list_merges("acme/proj"))

    err = exc_info.value
    assert err.status == status
    assert err.adapter == "gitlab"
    assert "gitlab.com/-/profile/personal_access_tokens" in err.remediation_url
    assert "gitlab.com/-/profile/personal_access_tokens" in str(err)


def test_non_auth_error_raises_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _HttpStub([(500, {"message": "boom"}, {})])
    monkeypatch.setattr(gitlab_module, "_http_get", stub)
    adapter = GitLabAdapter("tok_abc")
    with pytest.raises(RuntimeError, match="HTTP 500"):
        list(adapter.list_merges("acme/proj"))


def test_empty_token_rejected() -> None:
    with pytest.raises(ValueError):
        GitLabAdapter("")


def test_unsupported_auth_mode_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = GitLabAdapter("tok", auth_mode=AuthMode.PAT)
    adapter._auth_mode = "bogus"  # type: ignore[assignment]
    with pytest.raises(ValueError, match="Unsupported auth mode"):
        adapter._auth_headers()


@pytest.mark.parametrize("status", [401, 403])
def test_pr_context_commits_sub_call_auth_failure_propagates(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """Regression: a revoked token on the commits sub-call must raise
    ``AuthFailure`` instead of silently returning an empty commit list.
    """
    stub = _HttpStub(
        [
            (200, _MR_JSON, {}),
            (status, {"message": "forbidden"}, {}),
        ]
    )
    monkeypatch.setattr(gitlab_module, "_http_get", stub)

    adapter = GitLabAdapter("tok_abc", auth_mode=AuthMode.PAT)
    with pytest.raises(AuthFailure) as exc_info:
        adapter.pr_context("acme/proj", 42)
    assert exc_info.value.status == status
    assert exc_info.value.adapter == "gitlab"


def test_pr_context_commits_sub_call_non_auth_error_is_best_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-auth error on the commits sub-call still yields a context dict.

    Only auth failures are fatal on the sub-call; other transient errors
    (HTTP 500, invalid payload) are absorbed so the primary MR payload is
    still returned with an empty ``commit_shas`` tuple.
    """
    stub = _HttpStub(
        [
            (200, _MR_JSON, {}),
            (500, {"message": "boom"}, {}),
        ]
    )
    monkeypatch.setattr(gitlab_module, "_http_get", stub)

    adapter = GitLabAdapter("tok_abc", auth_mode=AuthMode.PAT)
    ctx = adapter.pr_context("acme/proj", 42)
    assert ctx["commit_shas"] == ()
    # Main MR payload still populated.
    assert ctx["title"] == "Fix widget alignment"
