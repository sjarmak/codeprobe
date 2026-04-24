"""Shared stdlib-only HTTP GET helper for VCS and tracker adapters.

This module factors out the duplicated ``_http_get`` implementations that
previously lived in both :mod:`codeprobe.mining.vcs.gitlab` and
:mod:`codeprobe.mining.trackers.jira`. Keeping a single canonical
implementation means:

- Response-body decoding behaves identically across adapters.
- Error-body handling (non-UTF-8 bytes, invalid JSON) is exercised by one
  set of tests.
- Future fixes (e.g. retry, gzip support) land in one place.

ZFC note: pure mechanism — this is IO + JSON parsing with a safe fallback.
No semantic judgment happens here.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from codeprobe.net import guard_offline

__all__ = ["stdlib_get"]


def stdlib_get(
    url: str, headers: dict[str, str], timeout: float = 15.0
) -> tuple[int, Any, dict[str, str]]:
    """Perform a stdlib-only HTTP GET.

    Returns ``(status, parsed_body, response_headers)``. Non-2xx
    :class:`urllib.error.HTTPError` responses are caught so that callers
    can inspect the status code (e.g. 401/403 → AuthFailureError) without
    bespoke exception handling.

    The response body is parsed as UTF-8 JSON. If decoding fails (non-UTF-8
    bytes, truncated data, or non-JSON payload), the function returns a
    safe fallback dict ``{"_raw": <text-preview>}`` instead of raising —
    adapters decide how to surface that to the user.
    """
    # Offline gate: fail loud when ``CODEPROBE_OFFLINE`` is set so
    # mining/tracker adapters (GitLab, Jira, ...) don't silently try to
    # reach the public internet in airgapped runs.
    guard_offline(f"vcs/tracker HTTP GET {url}")

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (validated URL)
            raw = resp.read()
            status = resp.getcode()
            resp_headers = {k: v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        raw = exc.read() if hasattr(exc, "read") else b""
        status = exc.code
        resp_headers = {
            k: v for k, v in (exc.headers.items() if exc.headers else [])
        }
    try:
        body: Any = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        body = {"_raw": raw[:2048].decode("utf-8", errors="replace")}
    return status, body, resp_headers
