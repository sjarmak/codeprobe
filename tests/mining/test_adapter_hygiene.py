"""Zero-tolerance token-leak CI gate for VCS / tracker adapters.

Plants CODEPROBE_TEST_TOKEN in env, invokes each adapter with a stubbed HTTP
client, and asserts the token value does NOT appear in any emitted log record
(message or args or extra payload) and is redacted in any returned payload.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest

from codeprobe.mining.trackers import jira as jira_module
from codeprobe.mining.trackers.jira import JiraAdapter
from codeprobe.mining.vcs import gitlab as gitlab_module
from codeprobe.mining.vcs.base import AuthMode, redact
from codeprobe.mining.vcs.gitlab import GitLabAdapter


@pytest.fixture
def planted_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """Plant a random token in CODEPROBE_TEST_TOKEN and return its value."""
    tok = f"tkn_{uuid.uuid4().hex}"
    monkeypatch.setenv("CODEPROBE_TEST_TOKEN", tok)
    return tok


class _CapturingHandler(logging.Handler):
    """Captures every LogRecord so we can scan for leaked tokens."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def contains(self, needle: str) -> bool:
        for rec in self.records:
            try:
                formatted = rec.getMessage()
            except Exception:
                formatted = rec.msg if isinstance(rec.msg, str) else repr(rec.msg)
            haystacks: list[Any] = [formatted, rec.msg, rec.args]
            if hasattr(rec, "payload"):
                haystacks.append(rec.payload)
            for h in haystacks:
                if _deep_contains(h, needle):
                    return True
        return False


def _deep_contains(obj: Any, needle: str) -> bool:
    if obj is None:
        return False
    if isinstance(obj, str):
        return needle in obj
    if isinstance(obj, dict):
        return any(
            _deep_contains(k, needle) or _deep_contains(v, needle)
            for k, v in obj.items()
        )
    if isinstance(obj, (list, tuple, set)):
        return any(_deep_contains(x, needle) for x in obj)
    return needle in repr(obj)


def _attach_capture() -> tuple[_CapturingHandler, list[logging.Logger]]:
    handler = _CapturingHandler()
    loggers = [
        logging.getLogger("codeprobe"),
        logging.getLogger("codeprobe.mining.vcs.gitlab"),
        logging.getLogger("codeprobe.mining.trackers.jira"),
        logging.getLogger(),  # root
    ]
    for lg in loggers:
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
    return handler, loggers


def _detach_capture(handler: _CapturingHandler, loggers: list[logging.Logger]) -> None:
    for lg in loggers:
        lg.removeHandler(handler)


class _EchoStub:
    """HTTP stub that echoes the planted token into response bodies + headers."""

    def __init__(self, token: str, response: Any):
        self.token = token
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, url: str, headers: dict[str, str], timeout: float = 15.0
    ) -> tuple[int, Any, dict[str, str]]:
        self.calls.append({"url": url, "headers": dict(headers)})
        return (200, self.response, {"X-Echo-Token": self.token})


def test_redact_helper_masks_known_tokens() -> None:
    tok = "tkn_secret_123"
    obj = {"auth": f"Bearer {tok}", "nested": [{"k": tok}], "safe": "ok"}
    out = redact(obj, {tok})
    assert tok not in repr(out)
    assert out["auth"] == "Bearer [REDACTED-TOKEN]"
    assert out["nested"][0]["k"] == "[REDACTED-TOKEN]"
    assert out["safe"] == "ok"


def test_redact_helper_longer_token_first() -> None:
    # Ensure overlapping tokens are handled: the longer token should be
    # replaced in full rather than leaving a partial dangling suffix.
    short = "abc"
    long_ = "abcDEF"
    out = redact(f"value={long_} and short={short}", {short, long_})
    assert out == "value=[REDACTED-TOKEN] and short=[REDACTED-TOKEN]"


def test_gitlab_adapter_does_not_leak_token(
    monkeypatch: pytest.MonkeyPatch, planted_token: str
) -> None:
    # The adapter is constructed with the planted token as its auth token.
    response_body = [
        {
            "id": 1,
            "iid": 1,
            "title": f"Has {planted_token} embedded in title",
            "description": f"Accidental leak: {planted_token}",
            "state": "merged",
            "source_branch": "a",
            "target_branch": "b",
            "merge_commit_sha": "sha",
            "web_url": "https://gitlab.com/x/y/-/merge_requests/1",
            "author": {"username": "alice"},
            "labels": [f"tag-{planted_token}"],
        }
    ]
    stub = _EchoStub(planted_token, response_body)
    monkeypatch.setattr(gitlab_module, "_http_get", stub)

    handler, loggers = _attach_capture()
    try:
        adapter = GitLabAdapter(planted_token, auth_mode=AuthMode.PAT)

        # Explicit contract: redact_request / redact_response mask the token.
        safe_req = adapter.redact_request(
            {"headers": {"PRIVATE-TOKEN": planted_token}, "body": planted_token}
        )
        assert _deep_contains(safe_req, "[REDACTED-TOKEN]")
        assert not _deep_contains(safe_req, planted_token)

        results = list(adapter.list_merges("x/y"))
        assert len(results) == 1

        # pr_context makes two GETs (MR + commits); install a stub that serves
        # both from canned payloads, both containing the planted token.
        ctx_response = dict(response_body[0])
        commits_response = [{"id": f"sha-{planted_token}"}]
        monkeypatch.setattr(
            gitlab_module,
            "_http_get",
            _make_two_call_stub(planted_token, ctx_response, commits_response),
        )
        ctx = adapter.pr_context("x/y", 1)
        assert ctx["enrichment_source"] == "gitlab"
        assert not _deep_contains(ctx, planted_token)

        # Any caller looking at the emitted logs must not see the raw token.
        assert not handler.contains(planted_token), (
            "GitLabAdapter leaked token into log records — check _log/redact wiring"
        )
    finally:
        _detach_capture(handler, loggers)


def _make_two_call_stub(token: str, first: Any, second: Any):
    responses = [first, second]

    def _call(url: str, headers: dict[str, str], timeout: float = 15.0):
        resp = responses.pop(0) if responses else {}
        return (200, resp, {"X-Echo-Token": token})

    return _call


def test_jira_adapter_does_not_leak_token(
    monkeypatch: pytest.MonkeyPatch, planted_token: str
) -> None:
    response_body = {
        "key": "PROJ-1",
        "fields": {
            "summary": f"Leaky title {planted_token}",
            "status": {"name": "Open"},
            "description": f"Body with {planted_token}",
        },
    }
    stub = _EchoStub(planted_token, response_body)
    monkeypatch.setattr(jira_module, "_http_get", stub)

    handler, loggers = _attach_capture()
    try:
        adapter = JiraAdapter(
            planted_token,
            base_url="https://acme.atlassian.net",
            auth_mode=AuthMode.PAT,
        )

        safe_req = adapter.redact_request(
            {"headers": {"Authorization": f"Bearer {planted_token}"}}
        )
        assert _deep_contains(safe_req, "[REDACTED-TOKEN]")
        assert not _deep_contains(safe_req, planted_token)

        ticket = adapter.fetch_ticket("PROJ-1")
        # The ticket body WILL include the token because the real API response
        # contained it — that is expected upstream data. The contract is that
        # we must not leak it into LOGS. (Callers writing the ticket to disk
        # should run it through redact_response if they want masked output.)
        assert ticket.key == "PROJ-1"

        # Redacted response path strips the token.
        masked = adapter.redact_response({"body": ticket.body, "title": ticket.title})
        assert not _deep_contains(masked, planted_token)
        assert _deep_contains(masked, "[REDACTED-TOKEN]")

        assert not handler.contains(planted_token), (
            "JiraAdapter leaked token into log records — check _log/redact wiring"
        )
    finally:
        _detach_capture(handler, loggers)


def test_adapters_pick_up_token_from_env(
    monkeypatch: pytest.MonkeyPatch, planted_token: str
) -> None:
    """The planted env token must be added to _known_tokens automatically."""
    gl = GitLabAdapter("different_auth_tok")
    assert planted_token in gl._known_tokens

    jira = JiraAdapter("different_auth_tok", base_url="https://acme.atlassian.net")
    assert planted_token in jira._known_tokens
