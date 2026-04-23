"""Networking & credential diagnostics for codeprobe.

This package holds small, pure-IO helpers used by the offline pre-flight
(``codeprobe check-infra offline``) to decide whether an airgapped run
should even be started. It intentionally contains no semantic judgment —
just reads credentials, parses expirations, and returns structured data
for the CLI to render.
"""

from __future__ import annotations

from codeprobe.net.credential_ttl import (
    CredentialTTLError,
    get_credential_ttl,
)

__all__ = [
    "CredentialTTLError",
    "get_credential_ttl",
]
