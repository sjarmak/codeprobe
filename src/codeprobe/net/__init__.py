"""Networking & credential diagnostics for codeprobe.

This package holds small, pure-IO helpers used by the offline pre-flight
(``codeprobe check-infra offline``) to decide whether an airgapped run
should even be started. It intentionally contains no semantic judgment —
just reads credentials, parses expirations, and returns structured data
for the CLI to render.
"""

from __future__ import annotations

import os

from codeprobe.net.credential_ttl import (
    CredentialTTLError,
    get_credential_ttl,
)

__all__ = [
    "CredentialTTLError",
    "get_credential_ttl",
    "is_offline_mode",
]


def is_offline_mode() -> bool:
    """Return True when ``CODEPROBE_OFFLINE`` env var signals offline mode.

    Set by ``codeprobe run --offline`` for subprocesses so downstream
    network-touching modules can short-circuit their calls. The current
    wiring is opt-in per subsystem — individual callers must check this
    helper themselves. See docs/reviews/v0.6.0-batch-c.md for the
    follow-up retrofit plan.

    Truthy values: ``"1"``, ``"true"``, ``"yes"``, ``"on"`` (case-insensitive).
    Any other value (including unset, empty, ``"0"``, ``"false"``) is
    treated as "not offline".
    """
    raw = os.environ.get("CODEPROBE_OFFLINE", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}
