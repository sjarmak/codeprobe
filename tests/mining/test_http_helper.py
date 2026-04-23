"""Tests for the shared stdlib HTTP helper at
:mod:`codeprobe.mining.vcs._http`.

These exercises guard the fallback that keeps both :class:`GitLabAdapter`
and :class:`JiraAdapter` alive when a server returns a non-UTF-8 or
malformed JSON error body.
"""

from __future__ import annotations

import io
import urllib.error
from typing import Any
from unittest.mock import patch

from codeprobe.mining.vcs._http import stdlib_get


def _fake_urlopen(body: bytes, status: int = 200):
    class _Resp:
        def __init__(self) -> None:
            self._body = body
            self.headers = {"Content-Type": "application/octet-stream"}

        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def read(self) -> bytes:
            return self._body

        def getcode(self) -> int:
            return status

    return lambda req, timeout=15.0: _Resp()


def test_stdlib_get_returns_parsed_json_on_success() -> None:
    with patch(
        "codeprobe.mining.vcs._http.urllib.request.urlopen",
        side_effect=_fake_urlopen(b'{"ok": true, "n": 7}'),
    ):
        status, body, headers = stdlib_get(
            "https://example.invalid/foo", {"Accept": "application/json"}
        )
    assert status == 200
    assert body == {"ok": True, "n": 7}
    assert "Content-Type" in headers


def test_stdlib_get_safe_fallback_on_non_utf8_body() -> None:
    """Non-UTF-8 bytes in the response body must not crash — the helper
    returns a structured ``{"_raw": ...}`` dict so adapters can still
    report a meaningful error without losing the status code.
    """
    non_utf8 = b"\xff\xfe\x80abc"
    with patch(
        "codeprobe.mining.vcs._http.urllib.request.urlopen",
        side_effect=_fake_urlopen(non_utf8, status=200),
    ):
        status, body, _ = stdlib_get("https://example.invalid/foo", {})
    assert status == 200
    assert isinstance(body, dict)
    assert "_raw" in body
    # The replacement-encoded preview should be a string.
    assert isinstance(body["_raw"], str)


def test_stdlib_get_safe_fallback_on_malformed_json_body() -> None:
    with patch(
        "codeprobe.mining.vcs._http.urllib.request.urlopen",
        side_effect=_fake_urlopen(b"<html>not json</html>", status=200),
    ):
        status, body, _ = stdlib_get("https://example.invalid/foo", {})
    assert status == 200
    assert isinstance(body, dict)
    assert body["_raw"].startswith("<html>")


def test_stdlib_get_captures_http_error_status() -> None:
    """HTTPError responses must be caught so adapters can read the status."""
    err = urllib.error.HTTPError(
        url="https://example.invalid/foo",
        code=403,
        msg="forbidden",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b'{"message": "no"}'),
    )

    def _raise(req, timeout=15.0):  # noqa: ARG001
        raise err

    with patch(
        "codeprobe.mining.vcs._http.urllib.request.urlopen", side_effect=_raise
    ):
        status, body, _ = stdlib_get("https://example.invalid/foo", {})
    assert status == 403
    assert body == {"message": "no"}


def test_stdlib_get_http_error_with_non_utf8_body() -> None:
    """Regression: the jira/gitlab adapters both depend on the shared
    helper's ability to swallow non-UTF-8 bytes on the error path — the
    old gitlab version was missing ``UnicodeDecodeError`` from the catch
    list and would crash on such bodies.
    """
    err = urllib.error.HTTPError(
        url="https://example.invalid/foo",
        code=500,
        msg="boom",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b"\xff\xfe\x80"),
    )

    def _raise(req, timeout=15.0):  # noqa: ARG001
        raise err

    with patch(
        "codeprobe.mining.vcs._http.urllib.request.urlopen", side_effect=_raise
    ):
        status, body, _ = stdlib_get("https://example.invalid/foo", {})
    assert status == 500
    assert isinstance(body, dict)
    assert "_raw" in body
