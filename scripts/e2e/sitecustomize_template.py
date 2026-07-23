"""Template copied verbatim into a venv's site-packages as ``sitecustomize.py``.

Python auto-imports ``sitecustomize`` at interpreter startup (before user
code runs), so once this file is placed in the harness venv's
``site-packages/``, every subprocess started with that venv's Python gets
the guard for free — no import needed in the harness or in codeprobe
itself.

Blocks any non-loopback ``socket`` connect/getaddrinfo call and appends one
line per blocked attempt to the file named by the ``CODEPROBE_E2E_NETLOG``
env var (never raises — the ``--no-llm`` guarantee is "made zero calls",
which the harness verifies by asserting the log file stays empty, not by
crashing mid-mine on the first offender and losing the rest of the
evidence). Loopback traffic (the Dolt-backed beads store, a local test
HTTP server, etc.) is left untouched.

Ported from the in-process pytest fixture at
``tests/cli/test_offline_e2e.py::airgap`` — same four monkeypatch targets
and the same loopback allowlist, adapted from "raise" to "log" and from a
pytest fixture to a real site-packages file (stdlib only, no pytest
import — this runs in a fresh venv with no dev dependencies installed).
"""

from __future__ import annotations

import os
import socket

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
_LOG_PATH = os.environ.get("CODEPROBE_E2E_NETLOG")


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


def _log_blocked(kind: str, target: object) -> None:
    if not _LOG_PATH:
        return
    with open(_LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(f"{kind} {target!r}\n")


_original_create_connection = socket.create_connection
_original_getaddrinfo = socket.getaddrinfo
_original_socket_connect = socket.socket.connect
_original_socket_connect_ex = socket.socket.connect_ex


def _guarded_create_connection(address, *args, **kwargs):  # type: ignore[no-untyped-def]
    host = address[0] if isinstance(address, tuple) else address
    if not _is_local_host(host):
        _log_blocked("create_connection", address)
        raise OSError(f"codeprobe-e2e sitecustomize guard blocked connect to {address!r}")
    return _original_create_connection(address, *args, **kwargs)


def _guarded_getaddrinfo(host, *args, **kwargs):  # type: ignore[no-untyped-def]
    if not _is_local_host(host):
        _log_blocked("getaddrinfo", host)
        raise OSError(f"codeprobe-e2e sitecustomize guard blocked getaddrinfo for {host!r}")
    return _original_getaddrinfo(host, *args, **kwargs)


def _guarded_socket_connect(self, address):  # type: ignore[no-untyped-def]
    host = address[0] if isinstance(address, tuple) else address
    if not _is_local_host(host):
        _log_blocked("connect", address)
        raise OSError(f"codeprobe-e2e sitecustomize guard blocked connect to {address!r}")
    return _original_socket_connect(self, address)


def _guarded_socket_connect_ex(self, address):  # type: ignore[no-untyped-def]
    host = address[0] if isinstance(address, tuple) else address
    if not _is_local_host(host):
        _log_blocked("connect_ex", address)
        return 111  # ECONNREFUSED — connect_ex returns an errno, never raises
    return _original_socket_connect_ex(self, address)


socket.create_connection = _guarded_create_connection
socket.getaddrinfo = _guarded_getaddrinfo
socket.socket.connect = _guarded_socket_connect
socket.socket.connect_ex = _guarded_socket_connect_ex
