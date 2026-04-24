"""Jira issue tracker adapter (REST API v3).

Auth: PAT or OAuth 2.0. Both materialize as ``Authorization: Bearer <token>``.
Outgoing / incoming payloads are redacted before any log / event write.
HTTP 401/403 → :class:`AuthFailure` (fail-loud, no fallback).
"""

from __future__ import annotations

import logging
import os
import urllib.parse
from typing import Any, cast

from codeprobe.mining.trackers.base import Ticket
from codeprobe.mining.vcs._http import stdlib_get
from codeprobe.mining.vcs.base import (
    AuthFailure,
    AuthMode,
    RedactingLoggerMixin,
    redact,
)

__all__ = ["JiraAdapter"]

_REMEDIATION_URL = "https://id.atlassian.com/manage-profile/security/api-tokens"
_TOKEN_ENV_VARS = (
    "CODEPROBE_TEST_TOKEN",
    "JIRA_TOKEN",
    "JIRA_API_TOKEN",
    "JIRA_OAUTH_TOKEN",
)

logger = logging.getLogger(__name__)


# Module-level alias kept so tests can monkeypatch ``jira_module._http_get``.
# The canonical implementation lives in :mod:`codeprobe.mining.vcs._http`.
_http_get = stdlib_get


class JiraAdapter(RedactingLoggerMixin):
    """IssueAdapter implementation for Jira Cloud."""

    name = "jira"

    def __init__(
        self,
        token: str,
        *,
        base_url: str,
        auth_mode: AuthMode = AuthMode.PAT,
    ) -> None:
        if not token:
            raise ValueError(
                "JiraAdapter requires a non-empty token. "
                f"Create one at {_REMEDIATION_URL}"
            )
        if not base_url:
            raise ValueError("JiraAdapter requires base_url (e.g. https://acme.atlassian.net)")
        self._token = token
        self._auth_mode = auth_mode
        self._base_url = base_url.rstrip("/")
        self._logger = logger

        self._known_tokens: set[str] = {token}
        for env_var in _TOKEN_ENV_VARS:
            val = os.environ.get(env_var)
            if val:
                self._known_tokens.add(val)

    # ------------------------------------------------------------------ auth
    def _auth_headers(self) -> dict[str, str]:
        # Both PAT and OAuth 2.0 surface as a Bearer header at the transport
        # layer; only the token source differs. Keeping a single code path
        # avoids divergent bugs between the two auth modes.
        if self._auth_mode in (AuthMode.PAT, AuthMode.OAUTH2):
            return {"Authorization": f"Bearer {self._token}"}
        raise ValueError(f"Unsupported auth mode: {self._auth_mode!r}")

    def _check_auth(self, status: int) -> None:
        if status in (401, 403):
            raise AuthFailure(self.name, status, _REMEDIATION_URL)

    # --------------------------------------------------------------- redact
    def redact_request(self, req: dict[str, Any]) -> dict[str, Any]:
        return cast("dict[str, Any]", redact(req, self._known_tokens))

    def redact_response(self, resp: dict[str, Any]) -> dict[str, Any]:
        return cast("dict[str, Any]", redact(resp, self._known_tokens))

    # ------------------------------------------------------------------ api
    def fetch_ticket(self, ref: str) -> Ticket:
        """Fetch a single ticket by key (``PROJ-123``)."""
        if not ref:
            raise ValueError("fetch_ticket requires a non-empty ticket ref")
        key = urllib.parse.quote(ref, safe="")
        url = f"{self._base_url}/rest/api/3/issue/{key}"
        headers = {"Accept": "application/json", **self._auth_headers()}

        safe_req = self.redact_request({"method": "GET", "url": url, "headers": headers})
        self._log(logging.DEBUG, "jira request: %s", safe_req)

        status, body, _ = _http_get(url, headers)
        self._check_auth(status)
        if status >= 400:
            safe_resp = self.redact_response({"status": status, "body": body})
            self._log(logging.ERROR, "jira error: %s", safe_resp)
            raise RuntimeError(f"Jira API error: HTTP {status}")

        if not isinstance(body, dict):
            raise RuntimeError(f"Jira returned non-dict payload: {type(body).__name__}")

        fields = body.get("fields") or {}
        title = str(fields.get("summary", "") or "")
        status_name = str(((fields.get("status") or {}).get("name", "")) or "")
        body_text = _extract_body(fields.get("description"))

        ticket = Ticket(
            key=str(body.get("key", ref)),
            title=title,
            body=body_text,
            status=status_name,
        )
        # Log redacted shape only — never the raw body.
        self._log(
            logging.DEBUG,
            "jira ticket fetched: %s",
            self.redact_response({"key": ticket.key, "status": ticket.status}),
        )
        return ticket


def _extract_body(description: Any) -> str:
    """Flatten Jira's description (may be string or ADF doc) into plain text."""
    if description is None:
        return ""
    if isinstance(description, str):
        return description
    if isinstance(description, dict):
        # Atlassian Document Format: walk content tree collecting "text" leaves.
        out: list[str] = []

        def _walk(node: Any) -> None:
            if isinstance(node, dict):
                if node.get("type") == "text" and isinstance(node.get("text"), str):
                    out.append(node["text"])
                for child in node.get("content", []) or []:
                    _walk(child)
            elif isinstance(node, list):
                for child in node:
                    _walk(child)

        _walk(description)
        return "\n".join(out).strip()
    return str(description)
