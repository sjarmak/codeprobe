"""Credential time-to-live probes per LLM backend.

``get_credential_ttl(backend_name)`` returns:

* ``None`` when the backend credential has no expiration (Anthropic direct
  API keys, user-managed ``openai_compat`` keys).
* ``timedelta(0)`` when the credential is present but already expired.
* ``timedelta(...)`` positive when the credential is present and valid,
  with the delta representing remaining lifetime.

The probe is intentionally conservative: it reads environment variables
and, where available, parses expiration timestamps from them. It does
NOT perform network IO — the offline pre-flight must work in an airgapped
VM. Callers (like ``codeprobe check-infra offline``) decide what to do
with a ``None`` (treat as "no-expiry") vs a concrete ``timedelta``.

Supported backends (matching ``codeprobe.llm.backends.BACKEND_CLASSES``):

* ``anthropic``     — API keys don't expire → ``None``
* ``bedrock``       — checks ``AWS_SESSION_EXPIRATION`` /
  ``AWS_CREDENTIAL_EXPIRATION`` env vars for STS session TTL
* ``vertex``        — checks ``GOOGLE_APPLICATION_CREDENTIALS_TOKEN_EXPIRY``
  env var (set by gcloud when a short-lived access token is minted)
* ``azure_openai``  — checks ``AZURE_TOKEN_EXPIRES_ON`` env var
* ``openai_compat`` — user-managed keys → ``None``
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

__all__ = [
    "CredentialTTLError",
    "get_credential_ttl",
    "KNOWN_BACKENDS",
]


class CredentialTTLError(Exception):
    """Raised when a backend is unknown or a credential timestamp is malformed."""


# The set of backends this module knows how to probe. Mirrors
# ``codeprobe.llm.backends.BACKEND_CLASSES`` keys — kept as a tuple so it
# can be consumed as a structural constant without a runtime import.
KNOWN_BACKENDS: tuple[str, ...] = (
    "anthropic",
    "bedrock",
    "vertex",
    "azure_openai",
    "openai_compat",
)


def _parse_iso_utc(raw: str) -> datetime:
    """Parse an ISO-8601 timestamp into a timezone-aware UTC datetime.

    Accepts trailing ``Z`` (Zulu) and ``+00:00`` suffixes. Raises
    :class:`CredentialTTLError` on anything else — we do not guess.
    """
    cleaned = raw.strip()
    if not cleaned:
        raise CredentialTTLError("empty credential expiration timestamp")
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise CredentialTTLError(
            f"unparseable credential expiration {raw!r}: {exc}"
        ) from exc
    if dt.tzinfo is None:
        # Naive timestamps are interpreted as UTC — matches AWS/GCP/Azure
        # convention when the suffix was dropped by a downstream tool.
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _ttl_from_env(env_var: str, *, now: datetime | None = None) -> timedelta | None:
    """Return a :class:`timedelta` remaining until ``env_var`` expires.

    Returns ``None`` when the env var is unset (the caller decides whether
    "unset" means no-expiry or not-configured).
    Returns ``timedelta(0)`` when the timestamp is in the past.
    """
    raw = os.environ.get(env_var)
    if raw is None or raw == "":
        return None
    expires_at = _parse_iso_utc(raw)
    current = now if now is not None else datetime.now(tz=UTC)
    remaining = expires_at - current
    if remaining.total_seconds() <= 0:
        return timedelta(0)
    return remaining


def get_credential_ttl(
    backend_name: str, *, now: datetime | None = None
) -> timedelta | None:
    """Return remaining credential TTL for a backend, or ``None`` if N/A.

    ``None`` encodes "no expiration expected for this backend" (anthropic,
    openai_compat) *and* "no expiration advertised by this environment"
    (bedrock/vertex/azure where the env vars are unset). Callers treat
    ``None`` as a non-blocking condition — the offline pre-flight logs it
    as ``no-expiry`` and continues.

    A ``timedelta`` of zero or negative signals an already-expired
    credential and must be treated as a failure by callers.

    Raises :class:`CredentialTTLError` for unknown backends or malformed
    timestamps (validate-or-die at the trust boundary).
    """
    name = backend_name.strip().lower()
    if name not in KNOWN_BACKENDS:
        known = ", ".join(KNOWN_BACKENDS)
        raise CredentialTTLError(
            f"Unknown backend {backend_name!r}. Known backends: {known}"
        )

    if name == "anthropic":
        # API keys issued by Anthropic don't expire.
        return None
    if name == "openai_compat":
        # User-managed gateways — we can't introspect key lifetime.
        return None

    if name == "bedrock":
        # Prefer the AWS CLI convention (AWS_SESSION_EXPIRATION) and fall
        # back to the boto3/SDK convention (AWS_CREDENTIAL_EXPIRATION).
        for env_var in ("AWS_SESSION_EXPIRATION", "AWS_CREDENTIAL_EXPIRATION"):
            ttl = _ttl_from_env(env_var, now=now)
            if ttl is not None:
                return ttl
        return None

    if name == "vertex":
        # gcloud writes the active token expiry to this env var when a
        # short-lived access token is exported for a headless run.
        return _ttl_from_env(
            "GOOGLE_APPLICATION_CREDENTIALS_TOKEN_EXPIRY", now=now
        )

    if name == "azure_openai":
        # Azure CLI / azure-identity populates AZURE_TOKEN_EXPIRES_ON on
        # ``az account get-access-token`` flows. Accept ISO-8601 as the
        # canonical form.
        return _ttl_from_env("AZURE_TOKEN_EXPIRES_ON", now=now)

    # Exhaustive match guard — KNOWN_BACKENDS validation should prevent
    # reaching this branch.
    raise CredentialTTLError(  # pragma: no cover - defensive
        f"No TTL probe implemented for backend {backend_name!r}"
    )
