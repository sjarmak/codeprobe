"""GitLab VCS adapter — lists merge requests and builds PR context dicts.

Auth: PAT via ``PRIVATE-TOKEN`` header, OR OAuth 2.0 via ``Authorization: Bearer``.
All outgoing / incoming payloads are redacted before they reach any log or
event emitter. HTTP 401/403 → :class:`AuthFailure` (fail-loud, no fallback).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any

from codeprobe.mining.vcs.base import (
    AuthFailure,
    AuthMode,
    MergeRequest,
    RedactingLoggerMixin,
    redact,
)

__all__ = ["GitLabAdapter"]

_REMEDIATION_URL = "https://gitlab.com/-/profile/personal_access_tokens"
_DEFAULT_BASE = "https://gitlab.com"
_TOKEN_ENV_VARS = ("CODEPROBE_TEST_TOKEN", "GITLAB_TOKEN", "GITLAB_PAT", "GITLAB_OAUTH_TOKEN")

logger = logging.getLogger(__name__)


def _http_get(
    url: str, headers: dict[str, str], timeout: float = 15.0
) -> tuple[int, Any, dict[str, str]]:
    """Thin stdlib GET seam — module-level so tests can monkeypatch easily.

    Returns ``(status, parsed_body, response_headers)``. On a non-2xx HTTP
    response the ``HTTPError`` is caught and its body parsed so that the
    adapter can raise :class:`AuthFailure` with the real status code.
    """
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (validated URL)
            raw = resp.read()
            status = resp.getcode()
            resp_headers = {k: v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        raw = exc.read() if hasattr(exc, "read") else b""
        status = exc.code
        resp_headers = {k: v for k, v in (exc.headers.items() if exc.headers else [])}
    try:
        body: Any = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        body = {"_raw": raw[:2048].decode("utf-8", errors="replace")}
    return status, body, resp_headers


class GitLabAdapter(RedactingLoggerMixin):
    """VCSAdapter implementation for GitLab (gitlab.com or self-hosted)."""

    name = "gitlab"

    def __init__(
        self,
        token: str,
        *,
        auth_mode: AuthMode = AuthMode.PAT,
        base_url: str = _DEFAULT_BASE,
    ) -> None:
        if not token:
            raise ValueError(
                "GitLabAdapter requires a non-empty token. "
                f"Create one at {_REMEDIATION_URL}"
            )
        self._token = token
        self._auth_mode = auth_mode
        self._base_url = base_url.rstrip("/")
        self._logger = logger

        # Known tokens = the live auth token + anything flagged via env. We
        # redact all of them even if only one is actually in use, so a stale
        # env value can never leak via a log line.
        self._known_tokens: set[str] = {token}
        for env_var in _TOKEN_ENV_VARS:
            val = os.environ.get(env_var)
            if val:
                self._known_tokens.add(val)

    # ------------------------------------------------------------------ auth
    def _auth_headers(self) -> dict[str, str]:
        if self._auth_mode is AuthMode.PAT:
            return {"PRIVATE-TOKEN": self._token}
        if self._auth_mode is AuthMode.OAUTH2:
            return {"Authorization": f"Bearer {self._token}"}
        raise ValueError(f"Unsupported auth mode: {self._auth_mode!r}")

    def _check_auth(self, status: int) -> None:
        if status in (401, 403):
            raise AuthFailure(self.name, status, _REMEDIATION_URL)

    # --------------------------------------------------------------- redact
    def redact_request(self, req: dict[str, Any]) -> dict[str, Any]:
        return redact(req, self._known_tokens)

    def redact_response(self, resp: dict[str, Any]) -> dict[str, Any]:
        return redact(resp, self._known_tokens)

    # ------------------------------------------------------------------ api
    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        url = f"{self._base_url}{path}{query}"
        headers = {"Accept": "application/json", **self._auth_headers()}

        safe_req = self.redact_request({"method": "GET", "url": url, "headers": headers})
        self._log(logging.DEBUG, "gitlab request: %s", safe_req)

        status, body, _resp_headers = _http_get(url, headers)
        self._check_auth(status)
        if status >= 400:
            safe_resp = self.redact_response({"status": status, "body": body})
            self._log(logging.ERROR, "gitlab error: %s", safe_resp)
            raise RuntimeError(f"GitLab API error: HTTP {status}")

        safe_resp = self.redact_response({"status": status, "body_keys": _shape(body)})
        self._log(logging.DEBUG, "gitlab response: %s", safe_resp)
        return body

    def list_merges(
        self, project: str, *, limit: int = 20
    ) -> Iterator[MergeRequest]:
        """Yield recently merged MRs for ``project`` (URL-encoded path or numeric id)."""
        project_ref = urllib.parse.quote(str(project), safe="")
        body = self._get(
            f"/api/v4/projects/{project_ref}/merge_requests",
            params={"state": "merged", "per_page": int(limit), "order_by": "updated_at"},
        )
        if not isinstance(body, list):
            raise RuntimeError(f"GitLab returned non-list MR payload: {type(body).__name__}")
        for item in body:
            yield _to_merge_request(item)

    def pr_context(self, project: str, mr_iid: int) -> dict[str, Any]:
        """Return a rich context dict for a single MR.

        The returned dict always carries ``enrichment_source='gitlab'`` so
        downstream writers can set :class:`TaskMetadata.enrichment_source`
        without re-deriving it.
        """
        project_ref = urllib.parse.quote(str(project), safe="")
        mr = self._get(f"/api/v4/projects/{project_ref}/merge_requests/{int(mr_iid)}")
        if not isinstance(mr, dict):
            raise RuntimeError(f"GitLab returned non-dict MR payload: {type(mr).__name__}")

        # Related commits (best effort — if this sub-call fails on auth we
        # still fail loud because it shares _check_auth).
        try:
            commits = self._get(
                f"/api/v4/projects/{project_ref}/merge_requests/{int(mr_iid)}/commits"
            )
        except RuntimeError:
            commits = []

        context = {
            "enrichment_source": "gitlab",
            "project": project,
            "mr_iid": mr.get("iid"),
            "title": mr.get("title", ""),
            "description": mr.get("description", ""),
            "state": mr.get("state", ""),
            "author": (mr.get("author") or {}).get("username", ""),
            "labels": tuple(mr.get("labels") or ()),
            "source_branch": mr.get("source_branch", ""),
            "target_branch": mr.get("target_branch", ""),
            "merge_commit_sha": mr.get("merge_commit_sha", ""),
            "web_url": mr.get("web_url", ""),
            "commit_shas": tuple(
                c.get("id", "") for c in (commits if isinstance(commits, list) else [])
            ),
        }
        # Double-apply redaction on the outgoing context so even if a caller
        # bypasses our logging, emitted events are clean.
        return self.redact_response(context)


def _to_merge_request(item: dict[str, Any]) -> MergeRequest:
    """Mechanical mapping from GitLab JSON → :class:`MergeRequest`."""
    author = (item.get("author") or {}).get("username", "") if isinstance(item, dict) else ""
    return MergeRequest(
        id=int(item.get("id", 0)),
        iid=int(item.get("iid", 0)),
        title=str(item.get("title", "")),
        description=str(item.get("description", "") or ""),
        state=str(item.get("state", "")),
        source_branch=str(item.get("source_branch", "")),
        target_branch=str(item.get("target_branch", "")),
        merge_commit_sha=str(item.get("merge_commit_sha", "") or ""),
        web_url=str(item.get("web_url", "")),
        author=author,
        labels=tuple(item.get("labels") or ()),
    )


def _shape(obj: Any) -> Any:
    """Return a compact structural summary for debug logging (no values)."""
    if isinstance(obj, dict):
        return sorted(obj.keys())
    if isinstance(obj, list):
        return [_shape(x) for x in obj[:1]] + ([f"...(+{len(obj) - 1})"] if len(obj) > 1 else [])
    return type(obj).__name__
