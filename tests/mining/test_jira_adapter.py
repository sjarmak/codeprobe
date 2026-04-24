"""Tests for codeprobe.mining.trackers.jira.JiraAdapter."""

from __future__ import annotations

from typing import Any

import pytest

from codeprobe.mining.trackers import jira as jira_module
from codeprobe.mining.trackers.base import Ticket
from codeprobe.mining.trackers.jira import JiraAdapter
from codeprobe.mining.vcs.base import AuthFailureError, AuthMode


class _HttpStub:
    def __init__(self, responses: list[tuple[int, Any, dict[str, str]]]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, url: str, headers: dict[str, str], timeout: float = 15.0
    ) -> tuple[int, Any, dict[str, str]]:
        self.calls.append({"url": url, "headers": dict(headers), "timeout": timeout})
        return self._responses.pop(0) if self._responses else (200, {}, {})


_JIRA_ISSUE = {
    "key": "PROJ-7",
    "fields": {
        "summary": "Button misaligned in header",
        "status": {"name": "In Progress"},
        "description": {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Steps: click button."},
                        {"type": "text", "text": "\nExpected: aligned."},
                    ],
                }
            ],
        },
    },
}


@pytest.mark.parametrize("mode", [AuthMode.PAT, AuthMode.OAUTH2])
def test_fetch_ticket_returns_ticket_dataclass(
    monkeypatch: pytest.MonkeyPatch, mode: AuthMode
) -> None:
    stub = _HttpStub([(200, _JIRA_ISSUE, {})])
    monkeypatch.setattr(jira_module, "_http_get", stub)

    adapter = JiraAdapter(
        "jira_tok_xyz", base_url="https://acme.atlassian.net", auth_mode=mode
    )
    ticket = adapter.fetch_ticket("PROJ-7")

    assert isinstance(ticket, Ticket)
    assert ticket.key == "PROJ-7"
    assert ticket.title == "Button misaligned in header"
    assert ticket.status == "In Progress"
    assert "click button" in ticket.body
    assert "aligned" in ticket.body

    # Both auth modes produce a Bearer header.
    assert len(stub.calls) == 1
    assert stub.calls[0]["headers"]["Authorization"] == "Bearer jira_tok_xyz"
    assert "rest/api/3/issue/PROJ-7" in stub.calls[0]["url"]


def test_fetch_ticket_accepts_plain_string_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "key": "PROJ-8",
        "fields": {
            "summary": "Legacy ticket",
            "status": {"name": "Done"},
            "description": "Plain text body",
        },
    }
    monkeypatch.setattr(jira_module, "_http_get", _HttpStub([(200, payload, {})]))
    adapter = JiraAdapter("tok", base_url="https://acme.atlassian.net")
    ticket = adapter.fetch_ticket("PROJ-8")
    assert ticket.body == "Plain text body"
    assert ticket.status == "Done"


@pytest.mark.parametrize("status", [401, 403])
def test_fetch_ticket_auth_failure(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setattr(
        jira_module, "_http_get", _HttpStub([(status, {"message": "nope"}, {})])
    )
    adapter = JiraAdapter("tok", base_url="https://acme.atlassian.net")
    with pytest.raises(AuthFailureError) as exc_info:
        adapter.fetch_ticket("PROJ-1")
    assert "id.atlassian.com/manage-profile/security/api-tokens" in str(exc_info.value)
    assert exc_info.value.adapter == "jira"
    assert exc_info.value.status == status


def test_fetch_ticket_non_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        jira_module, "_http_get", _HttpStub([(500, {"message": "boom"}, {})])
    )
    adapter = JiraAdapter("tok", base_url="https://acme.atlassian.net")
    with pytest.raises(RuntimeError, match="HTTP 500"):
        adapter.fetch_ticket("PROJ-1")


def test_empty_token_rejected() -> None:
    with pytest.raises(ValueError):
        JiraAdapter("", base_url="https://acme.atlassian.net")


def test_empty_base_url_rejected() -> None:
    with pytest.raises(ValueError):
        JiraAdapter("tok", base_url="")


def test_empty_ref_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jira_module, "_http_get", _HttpStub([]))
    adapter = JiraAdapter("tok", base_url="https://acme.atlassian.net")
    with pytest.raises(ValueError):
        adapter.fetch_ticket("")
