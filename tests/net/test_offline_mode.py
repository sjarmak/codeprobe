"""Tests for the ``guard_offline`` gate + adapter-level offline enforcement.

Covers acceptance criteria from the offline-subsystem-audit work unit:

* ``is_offline_mode()`` honors ``CODEPROBE_OFFLINE`` (truthy / falsy / unset).
* ``guard_offline()`` raises a ``DiagnosticError(code='OFFLINE_NET_ATTEMPT',
  terminal=True)`` when offline mode is active.
* A representative mining adapter call (Sourcegraph's ``find_references``)
  routes through the gate before any real HTTP IO, so the operator gets a
  structured error envelope instead of a silent network-in-airgap.

Socket-level interception is intentionally out of scope (PRD NG6 — the
gate is opt-in at each known call site).
"""

from __future__ import annotations

import pytest

from codeprobe.cli.errors import DiagnosticError
from codeprobe.net import guard_offline, is_offline_mode

# ---------------------------------------------------------------------------
# is_offline_mode() — env var semantics
# ---------------------------------------------------------------------------


def test_is_offline_mode_returns_false_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEPROBE_OFFLINE", raising=False)
    assert is_offline_mode() is False


def test_is_offline_mode_returns_false_when_env_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEPROBE_OFFLINE", "")
    assert is_offline_mode() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "Yes", "ON"])
def test_is_offline_mode_truthy_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("CODEPROBE_OFFLINE", value)
    assert is_offline_mode() is True


@pytest.mark.parametrize("value", ["0", "false", "False", "no", "off"])
def test_is_offline_mode_falsy_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("CODEPROBE_OFFLINE", value)
    assert is_offline_mode() is False


# ---------------------------------------------------------------------------
# guard_offline() — behavior gate
# ---------------------------------------------------------------------------


def test_guard_offline_raises_diagnostic_error_when_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEPROBE_OFFLINE", "1")
    with pytest.raises(DiagnosticError) as excinfo:
        guard_offline("unit-test-context")

    exc = excinfo.value
    assert exc.code == "OFFLINE_NET_ATTEMPT"
    assert exc.terminal is True
    # Message should mention offline + carry the context string so
    # operators can identify which subsystem tripped.
    assert "CODEPROBE_OFFLINE" in exc.message
    assert "unit-test-context" in exc.message
    # DiagnosticError requires a diagnose_cmd — verify it points at the
    # offline pre-flight per the error-codes catalog.
    assert "check-infra offline" in exc.diagnose_cmd


def test_guard_offline_without_context_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEPROBE_OFFLINE", "1")
    with pytest.raises(DiagnosticError) as excinfo:
        guard_offline()
    assert excinfo.value.code == "OFFLINE_NET_ATTEMPT"


def test_guard_offline_noop_when_online(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEPROBE_OFFLINE", raising=False)
    # Must not raise, must return None.
    assert guard_offline("whatever") is None


def test_guard_offline_noop_when_env_explicit_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEPROBE_OFFLINE", "0")
    assert guard_offline("whatever") is None


# ---------------------------------------------------------------------------
# Adapter-level integration — verify the gate fires BEFORE network IO.
# ---------------------------------------------------------------------------


def test_sg_ground_truth_raises_offline_net_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A representative mining adapter path (Sourcegraph find_references)
    must raise ``OFFLINE_NET_ATTEMPT`` before any ``requests.post`` call.

    We stub ``get_valid_token`` so the function reaches the gate without
    a real credential lookup, and assert ``requests.post`` is never
    invoked.
    """

    from codeprobe.mining import sg_ground_truth

    class _StubCached:
        access_token = "stub-token"

    def _fake_get_valid_token(sg_url: str, force_refresh: bool = False):
        return _StubCached()

    # Patch the lazy import target directly — ``_call_find_references``
    # imports ``sg_auth.get_valid_token`` inside its body, so we
    # monkeypatch the module attribute.
    from codeprobe.mining import sg_auth

    monkeypatch.setattr(sg_auth, "get_valid_token", _fake_get_valid_token)

    # Sentinel: if the gate fails to fire, ``requests.post`` would be
    # called — monkeypatch it to blow up so the test can distinguish.
    import requests

    def _forbidden_post(*args, **kwargs):  # pragma: no cover - sentinel
        raise AssertionError(
            "requests.post was called — offline gate did not fire "
            "before network IO"
        )

    monkeypatch.setattr(requests, "post", _forbidden_post)

    monkeypatch.setenv("CODEPROBE_OFFLINE", "1")

    with pytest.raises(DiagnosticError) as excinfo:
        sg_ground_truth._call_find_references(
            symbol="foo",
            defining_file="src/foo.py",
            repo_sg_name="github.com/owner/repo",
            sg_url="https://demo.sourcegraph.com",
        )

    assert excinfo.value.code == "OFFLINE_NET_ATTEMPT"
    assert excinfo.value.terminal is True


def test_vcs_http_get_raises_offline_net_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared ``stdlib_get`` helper used by GitLab / Jira adapters
    must also route through the gate."""
    from codeprobe.mining.vcs import _http

    # Sentinel: if the gate misses, urlopen would be invoked.
    def _forbidden_urlopen(*args, **kwargs):  # pragma: no cover - sentinel
        raise AssertionError(
            "urllib.request.urlopen was called — offline gate did not fire"
        )

    monkeypatch.setattr(
        _http.urllib.request, "urlopen", _forbidden_urlopen
    )
    monkeypatch.setenv("CODEPROBE_OFFLINE", "1")

    with pytest.raises(DiagnosticError) as excinfo:
        _http.stdlib_get(
            "https://gitlab.example.com/api/v4/projects",
            headers={"PRIVATE-TOKEN": "stub"},
        )

    assert excinfo.value.code == "OFFLINE_NET_ATTEMPT"


def test_guard_passes_allows_stdlib_get_to_proceed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When offline mode is off, stdlib_get proceeds past the gate to the
    real urllib call. We monkeypatch ``urlopen`` with a fake response so
    the test does no real network IO."""
    from codeprobe.mining.vcs import _http

    monkeypatch.delenv("CODEPROBE_OFFLINE", raising=False)

    class _FakeResp:
        headers: dict[str, str] = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"{}"

        def getcode(self):
            return 200

    def _fake_urlopen(req, timeout):
        return _FakeResp()

    monkeypatch.setattr(_http.urllib.request, "urlopen", _fake_urlopen)

    status, body, _ = _http.stdlib_get(
        "https://gitlab.example.com/api/v4/projects",
        headers={"PRIVATE-TOKEN": "stub"},
    )
    assert status == 200
    assert body == {}
