"""Sourcegraph auth: cached token store with env-var fallback.

Sourcegraph Cloud (sourcegraph.com) exposes Personal Access Tokens for API
auth but does not publicly document an OAuth device-code flow. This module
therefore implements a PAT cache with the following lifecycle:

1. ``SRC_ACCESS_TOKEN`` env var — takes precedence, never touches the cache
   (preserves CI and keeps ephemeral tokens out of disk).
2. ``~/.codeprobe/auth.json`` cache (file 0600, parent dir 0700).
3. ``device_code_flow()`` is a stable stub that raises ``NotImplementedError``
   so the shape is ready if Sourcegraph ships device-code auth.

CLI wiring (prompting the user to paste a PAT on first use, then writing the
cache) lives in a follow-up bead: ``codeprobe auth sourcegraph`` will call
``save_cached_token`` directly.

ZFC compliance: pure IO + schema validation. No semantic judgment.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "https://sourcegraph.com"
ENV_VAR = "SRC_ACCESS_TOKEN"


class AuthError(RuntimeError):
    """Raised when no valid Sourcegraph token can be obtained.

    Error messages must NEVER include token material.
    """


@dataclass(frozen=True)
class CachedToken:
    """An immutable Sourcegraph credential.

    Attributes:
        access_token: Bearer token used in ``Authorization: token <value>``.
        refresh_token: Optional refresh token (None for PATs).
        expires_at: Optional expiry timestamp (UTC). None means non-expiring
            (typical PAT behavior).
        endpoint: Sourcegraph instance URL this token is scoped to.
    """

    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    endpoint: str

    def is_expired(self) -> bool:
        """Return True if the token has a known past expiry.

        Tokens without an ``expires_at`` (e.g. PATs) are treated as
        non-expiring.
        """
        if self.expires_at is None:
            return False
        return datetime.now(UTC) >= self.expires_at


# ---------------------------------------------------------------------------
# Cache file layout
# ---------------------------------------------------------------------------


def _cache_path() -> Path:
    return Path(os.path.expanduser("~")) / ".codeprobe" / "auth.json"


def _serialize(token: CachedToken) -> dict[str, str | None]:
    return {
        "access_token": token.access_token,
        "refresh_token": token.refresh_token,
        "expires_at": (
            token.expires_at.astimezone(UTC).isoformat()
            if token.expires_at is not None
            else None
        ),
    }


def _deserialize(endpoint: str, data: dict[str, object]) -> CachedToken | None:
    """Validate-or-die on the cache entry shape. Returns None on any mismatch."""
    access = data.get("access_token")
    if not isinstance(access, str) or not access:
        return None
    refresh = data.get("refresh_token")
    if refresh is not None and not isinstance(refresh, str):
        return None
    expires_raw = data.get("expires_at")
    expires: datetime | None
    if expires_raw is None:
        expires = None
    elif isinstance(expires_raw, str):
        try:
            expires = datetime.fromisoformat(expires_raw)
        except ValueError:
            return None
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
    else:
        return None
    return CachedToken(
        access_token=access,
        refresh_token=refresh,
        expires_at=expires,
        endpoint=endpoint,
    )


def load_cached_token(endpoint: str = DEFAULT_ENDPOINT) -> CachedToken | None:
    """Load a cached token for *endpoint*. Returns None if missing/corrupt."""
    path = _cache_path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        logger.warning("auth cache unreadable at %s; ignoring", path)
        return None
    if not isinstance(raw, dict):
        return None
    sg = raw.get("sourcegraph")
    if not isinstance(sg, dict):
        return None
    entry = sg.get(endpoint)
    if not isinstance(entry, dict):
        return None
    return _deserialize(endpoint, entry)


def save_cached_token(token: CachedToken) -> None:
    """Persist *token* to the auth cache with 0600/0700 permissions.

    Merges with any existing entries for other endpoints.
    """
    path = _cache_path()
    parent = path.parent
    parent.mkdir(mode=0o700, exist_ok=True)
    # Ensure parent dir perms even if it pre-existed with wider permissions.
    os.chmod(parent, 0o700)

    existing: dict[str, object] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            existing = {}

    sg_section = existing.get("sourcegraph")
    if not isinstance(sg_section, dict):
        sg_section = {}
    sg_section[token.endpoint] = _serialize(token)
    existing["sourcegraph"] = sg_section

    # Write with restrictive perms from the start (avoid race where file is
    # briefly world-readable before chmod).
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(existing, f, indent=2, sort_keys=True)
    except Exception:
        # Cleanup on failure so we don't leave a partial file.
        try:
            path.unlink(missing_ok=True)
        finally:
            raise
    # Re-apply perms in case umask or prior file state interfered.
    os.chmod(path, 0o600)


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------


def device_code_flow(endpoint: str) -> CachedToken:
    """Device-code OAuth flow.

    Not implemented: Sourcegraph Cloud does not publicly expose a device-code
    endpoint at time of writing. The ``codeprobe auth sourcegraph`` CLI
    command will instead prompt for a PAT and call :func:`save_cached_token`
    directly. The signature is kept so a future pivot lands cleanly.
    """
    raise NotImplementedError(
        "Sourcegraph Cloud does not expose a device-code OAuth flow. "
        "Use `codeprobe auth sourcegraph` to paste a PAT, or set "
        f"{ENV_VAR}."
    )


def refresh_token(cached: CachedToken) -> CachedToken | None:
    """Refresh an expired token using its refresh token.

    Returns None when no refresh is possible (e.g. PAT without refresh token,
    or 401 from the refresh endpoint). Never raises for expected failures.
    """
    if cached.refresh_token is None:
        return None
    # Placeholder: if Sourcegraph exposes a refresh endpoint in future, this
    # is where the mechanical POST lives. For now PATs don't refresh.
    return None


def clear_cached_token(
    service: str = "sourcegraph",
    endpoint: str = DEFAULT_ENDPOINT,
) -> None:
    """Remove cached auth for *service* at *endpoint*.

    Silently succeeds if no cache exists or the entry is missing.
    """
    path = _cache_path()
    if not path.exists():
        return
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(raw, dict):
        return
    section = raw.get(service)
    if isinstance(section, dict) and endpoint in section:
        del section[endpoint]
        if not section:
            del raw[service]
        # Rewrite with restrictive perms; delete on write failure to avoid
        # leaving a truncated/empty file that corrupts all cached tokens.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(raw, f, indent=2, sort_keys=True)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        os.chmod(path, 0o600)


def get_valid_token(
    endpoint: str = DEFAULT_ENDPOINT,
    *,
    force_refresh: bool = False,
) -> CachedToken:
    """Resolve a usable token for *endpoint*.

    Order of precedence:
    1. ``SRC_ACCESS_TOKEN`` env var — synthesized on the fly, cache untouched.
    2. Cached token for *endpoint* if present and not expired.
    3. Expired cached token with a refresh token — attempt refresh, persist.

    When *force_refresh* is True, skip the cached-token-is-valid shortcut
    and attempt a refresh (useful after a 401 from the API).

    Raises :class:`AuthError` if none of the above yield a usable token.
    Error messages never include token material.
    """
    env_value = os.environ.get(ENV_VAR)
    if env_value:
        if force_refresh:
            raise AuthError(
                f"Token from {ENV_VAR} was rejected (401). "
                f"Rotate the environment variable or run `codeprobe auth sourcegraph`."
            )
        return CachedToken(
            access_token=env_value,
            refresh_token=None,
            expires_at=None,
            endpoint=endpoint,
        )

    cached = load_cached_token(endpoint)
    if cached is None:
        raise AuthError(
            f"No Sourcegraph credentials found for {endpoint}. "
            f"Set {ENV_VAR} or run `codeprobe auth sourcegraph`."
        )

    if not force_refresh and not cached.is_expired():
        return cached

    refreshed = refresh_token(cached)
    if refreshed is None:
        if force_refresh:
            raise AuthError(
                f"Sourcegraph token for {endpoint} was rejected and cannot "
                f"be refreshed. Set {ENV_VAR} or run `codeprobe auth sourcegraph`."
            )
        raise AuthError(
            f"Cached Sourcegraph token for {endpoint} is expired and cannot "
            f"be refreshed. Set {ENV_VAR} or run `codeprobe auth sourcegraph`."
        )
    save_cached_token(refreshed)
    return refreshed


__all__ = [
    "AuthError",
    "CachedToken",
    "DEFAULT_ENDPOINT",
    "ENV_VAR",
    "clear_cached_token",
    "device_code_flow",
    "get_valid_token",
    "load_cached_token",
    "refresh_token",
    "save_cached_token",
]
