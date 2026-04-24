"""End-to-end test for ``codeprobe check-infra offline`` in an airgapped VM.

Bead: r16-offline-check-infra, AC #3.

Simulates an airgapped environment by monkey-patching ``socket`` module
functions to reject any connect/getaddrinfo call that isn't to the
loopback interface. The ``offline`` subcommand must complete entirely
from local state (env vars + the LLM registry on disk) — no outbound
traffic — so it should pass the check with stubbed credentials.
"""

from __future__ import annotations

import socket
from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner

from codeprobe.cli.check_infra import check_infra


_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


class OutboundNetworkBlocked(RuntimeError):
    """Raised when an airgapped test attempts non-loopback network IO."""


def _is_local_host(host: object) -> bool:
    if isinstance(host, tuple):
        host = host[0] if host else ""
    if host in (None, ""):
        return True
    if isinstance(host, (bytes, bytearray)):
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:
            return False
    if not isinstance(host, str):
        return False
    return host in _LOCAL_HOSTS or host.startswith("127.")


@pytest.fixture
def airgap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block every non-loopback network call at the socket layer."""

    original_create_connection = socket.create_connection
    original_getaddrinfo = socket.getaddrinfo
    original_socket_connect = socket.socket.connect
    original_socket_connect_ex = socket.socket.connect_ex

    def guarded_create_connection(address, *args, **kwargs):  # type: ignore[no-untyped-def]
        host = address[0] if isinstance(address, tuple) else address
        if not _is_local_host(host):
            raise OutboundNetworkBlocked(
                f"airgap fixture blocked outbound connect to {address!r}"
            )
        return original_create_connection(address, *args, **kwargs)

    def guarded_getaddrinfo(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        if not _is_local_host(host):
            raise OutboundNetworkBlocked(
                f"airgap fixture blocked getaddrinfo for {host!r}"
            )
        return original_getaddrinfo(host, *args, **kwargs)

    def guarded_socket_connect(self, address):  # type: ignore[no-untyped-def]
        host = address[0] if isinstance(address, tuple) else address
        if not _is_local_host(host):
            raise OutboundNetworkBlocked(
                f"airgap fixture blocked socket.connect to {address!r}"
            )
        return original_socket_connect(self, address)

    def guarded_socket_connect_ex(self, address):  # type: ignore[no-untyped-def]
        host = address[0] if isinstance(address, tuple) else address
        if not _is_local_host(host):
            raise OutboundNetworkBlocked(
                f"airgap fixture blocked socket.connect_ex to {address!r}"
            )
        return original_socket_connect_ex(self, address)

    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_socket_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_socket_connect_ex)


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _clear_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "AWS_SESSION_EXPIRATION",
        "AWS_CREDENTIAL_EXPIRATION",
        "GOOGLE_APPLICATION_CREDENTIALS_TOKEN_EXPIRY",
        "AZURE_TOKEN_EXPIRES_ON",
    ):
        monkeypatch.delenv(var, raising=False)


def test_airgap_fixture_actually_blocks_outbound_traffic(
    airgap: None,
) -> None:
    """Sanity check: the fixture really does block non-loopback calls."""
    with pytest.raises(OutboundNetworkBlocked):
        socket.create_connection(("example.com", 80), timeout=1)


def test_airgap_fixture_still_allows_loopback(airgap: None) -> None:
    """getaddrinfo for localhost must still resolve — CLI runner uses it."""
    # No exception expected.
    socket.getaddrinfo("127.0.0.1", 80)


def test_offline_check_passes_in_airgapped_vm_with_stubbed_endpoints(
    monkeypatch: pytest.MonkeyPatch, airgap: None
) -> None:
    """AC #3: full end-to-end run in a simulated airgapped VM.

    With stubbed credential expiration env vars and zero outbound traffic
    allowed, ``codeprobe check-infra offline`` must complete and exit 0.
    """
    _clear_creds(monkeypatch)
    future = datetime.now(tz=timezone.utc) + timedelta(hours=8)
    # Stub every TTL-bearing backend so the pre-flight sees a healthy
    # matrix without any network IO.
    monkeypatch.setenv("AWS_SESSION_EXPIRATION", _iso(future))
    monkeypatch.setenv(
        "GOOGLE_APPLICATION_CREDENTIALS_TOKEN_EXPIRY", _iso(future)
    )
    monkeypatch.setenv("AZURE_TOKEN_EXPIRES_ON", _iso(future))

    result = CliRunner().invoke(
        check_infra,
        ["offline", "--expected-run-duration", "1h", "--no-json"],
    )

    assert result.exit_code == 0, (
        f"offline pre-flight failed in airgapped VM: "
        f"{result.output!r} exc={result.exception!r}"
    )
    assert "OK" in result.output


def test_offline_check_fails_in_airgapped_vm_when_ttl_too_short(
    monkeypatch: pytest.MonkeyPatch, airgap: None
) -> None:
    """Airgap fixture on + expired-ish Bedrock creds → failure, no network."""
    _clear_creds(monkeypatch)
    soon = datetime.now(tz=timezone.utc) + timedelta(minutes=5)
    monkeypatch.setenv("AWS_SESSION_EXPIRATION", _iso(soon))

    result = CliRunner().invoke(
        check_infra,
        [
            "offline",
            "--expected-run-duration",
            "1h",
            "--backend",
            "bedrock",
        ],
    )

    assert result.exit_code != 0
    combined = result.output + (result.stderr_bytes or b"").decode(
        errors="replace"
    )
    assert "bedrock" in combined.lower()


def test_offline_check_respects_backend_filter_with_no_network(
    monkeypatch: pytest.MonkeyPatch, airgap: None
) -> None:
    """Filtering to Anthropic-only in an airgapped VM still exits 0 and
    reports no-expiry without touching the network."""
    _clear_creds(monkeypatch)

    result = CliRunner().invoke(
        check_infra,
        [
            "offline",
            "--expected-run-duration",
            "6h",
            "--backend",
            "anthropic",
            "--backend",
            "openai_compat",
            "--no-json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "anthropic" in result.output
    assert "openai_compat" in result.output
    assert "no-expiry" in result.output
