"""Tenant ID derivation with git-worktree detection.

Implements the priority chain spelled out in the Agent-Friendly CLI PRD
(§12.Q10 and §13-T7). The one public entry point is :func:`derive_tenant`,
which returns a ``(tenant_id, source)`` tuple where ``source`` is one of
the documented variants ('env', 'url-override+user', 'git-remote+user',
'git-remote+user+worktree', 'cwd-hash+user', 'flag').

Every derived tenant id is validated through
``codeprobe.paths._validate_tenant_id`` before being returned so callers
can join it into state paths without further checks.

Design notes:

* Git subprocess calls are hard-capped at 2s via ``timeout=`` to avoid
  hangs on pathological filesystems or airgapped networks. Failures at
  that boundary propagate as "no remote found" — we never swallow the
  error *silently*; we log the fall-through via ``source`` value returned.
* ``DiagnosticError`` is imported lazily to tolerate a not-yet-merged
  sibling work unit (``codeprobe.cli.errors``). A minimal in-module
  fallback is used when the real class is unavailable so tests and
  downstream code can still rely on raising the CI guard.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from codeprobe.paths import _validate_tenant_id

__all__ = ["derive_tenant", "DiagnosticError"]

# Filesystem-safe cap. _validate_tenant_id does not enforce a length, but
# path-segment limits on common filesystems (~255 bytes) plus the need to
# combine the slug with ``@<user>`` and optional ``@<worktree-hash>``
# suffixes means we keep the base slug short. 51 + '@' + up to 12 char
# user = 64.
MAX_TENANT_LEN = 64
_MAX_SLUG_LEN = 51  # leaves 12 chars for '@user' suffix
_ALLOWED_RE = re.compile(r"[^A-Za-z0-9._+-]")


def _lazy_diagnostic_error() -> type:
    """Return the real ``DiagnosticError`` class or a local fallback.

    The sibling errors-module unit may not yet be merged into the worktree
    we run in, so import lazily and fall back to a minimal dataclass with
    the same constructor contract.
    """

    try:
        from codeprobe.cli.errors import (  # type: ignore[import-not-found]
            DiagnosticError as _Real,
        )

        return _Real
    except Exception:  # noqa: BLE001 — any import failure falls back
        return _FallbackDiagnosticError


@dataclass
class _FallbackDiagnosticError(Exception):
    """Minimal stand-in used until ``codeprobe.cli.errors`` lands.

    Mirrors the constructor kwargs the sibling module will expose so call
    sites remain forward-compatible. Subclasses :class:`Exception` so it
    can actually be raised.
    """

    code: str = ""
    terminal: bool = False
    diagnose_cmd: str | None = None
    next_steps: list[tuple[str, str]] = field(default_factory=list)
    message: str = ""

    def __post_init__(self) -> None:
        super().__init__(self.message or self.code)


# Expose ``DiagnosticError`` at module scope for callers and tests that
# want a stable symbol. The resolver is run at import time so the symbol
# matches whichever version is live in the process.
DiagnosticError = _lazy_diagnostic_error()


def _sanitize(value: str) -> str:
    """Replace disallowed characters with ``-`` and trim the result.

    Allowed: ``[A-Za-z0-9._+-]``. Collapses consecutive ``-`` runs.
    """

    cleaned = _ALLOWED_RE.sub("-", value)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-._")
    return cleaned or "unknown"


def _strip_userinfo(url: str) -> str:
    """Remove ``user[:password]@`` prefix from HTTP(S) URLs.

    Prevents credentials that live in a remote URL from leaking into a
    tenant slug. SSH ``git@host:...`` form is preserved by the caller's
    regex split, so this only touches ``://`` URLs.
    """

    match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)([^/@]*@)?(.*)$", url)
    if not match:
        return url
    scheme, _userinfo, rest = match.groups()
    return f"{scheme}{rest}"


def _normalize_slug(url: str) -> str:
    """Turn a remote URL into a filesystem-safe slug.

    Handles:

    * ``git@host:org/repo.git``   (SSH short form)
    * ``ssh://git@host/org/repo`` (SSH URL form)
    * ``https://host/org/repo.git`` (HTTPS, with optional ``user:pass@``)
    * Bare paths / other schemes — best-effort: strips scheme, sanitizes.
    """

    raw = url.strip()
    if not raw:
        return "unknown"

    # SSH short form: git@host:org/repo.git
    ssh_match = re.match(r"^[^@\s]+@([^:]+):(.+?)(?:\.git)?$", raw)
    if ssh_match:
        host, path = ssh_match.groups()
        parts = [host, *[p for p in path.split("/") if p]]
        return _sanitize("-".join(parts))

    # Scheme form (http, https, ssh, git)
    scheme_match = re.match(
        r"^[a-zA-Z][a-zA-Z0-9+.-]*://(.+)$", _strip_userinfo(raw)
    )
    if scheme_match:
        rest = scheme_match.group(1)
        # Remove any lingering userinfo, then split host from path
        rest = re.sub(r"^[^/@]*@", "", rest, count=1)
        if "/" in rest:
            host, _, path = rest.partition("/")
        else:
            host, path = rest, ""
        path = re.sub(r"\.git/?$", "", path)
        parts = [host, *[p for p in path.split("/") if p]]
        return _sanitize("-".join(parts))

    # Fallback: treat as path-ish
    cleaned = re.sub(r"\.git/?$", "", raw)
    return _sanitize(cleaned.replace("/", "-").replace(":", "-"))


def _apply_length_cap(slug: str) -> str:
    """Ensure the slug fits within :data:`_MAX_SLUG_LEN`.

    When the raw slug would blow past the cap (deeply-nested GitLab paths,
    say), we truncate and append a short hash of the full input so the
    final id is still unique per remote.
    """

    if len(slug) <= _MAX_SLUG_LEN:
        return slug
    digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:8]
    # Leave room for '-<8hex>' marker (9 chars) inside the cap
    truncated = slug[: _MAX_SLUG_LEN - 9].rstrip("-._")
    return f"{truncated}-{digest}"


def _resolve_user(env: Mapping[str, str]) -> str:
    """Return the sanitized user name from env, with 'unknown' fallback."""

    raw = env.get("USER") or env.get("USERNAME") or "unknown"
    cleaned = _sanitize(raw)
    # Keep user short so the final id stays under MAX_TENANT_LEN.
    return cleaned[:12] or "unknown"


def _try_git_remote(cwd: str | Path, timeout: float = 2.0) -> str | None:
    """Return ``origin`` remote URL for ``cwd``, or None when unavailable.

    Runs ``git -C <cwd> config --get remote.origin.url`` with a hard
    timeout. Any git-level failure (not-a-repo, missing remote, git not
    installed, timeout) is treated as "no remote".
    """

    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    return url or None


def _detect_worktree(
    cwd: str | Path, timeout: float = 2.0
) -> str | None:
    """Return the worktree toplevel path when ``cwd`` is inside one.

    Checks ``git rev-parse --git-dir``: in a linked worktree the git-dir
    resolves to ``.../.git/worktrees/<name>``. If so, we ask git for the
    worktree's ``show-toplevel`` (which is the worktree's own root, not
    the main repo's) and return its resolved absolute path.

    Returns None for the main repo, non-repos, or any git failure.
    """

    try:
        gitdir = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if gitdir.returncode != 0:
        return None
    if ".git/worktrees/" not in gitdir.stdout and ".git\\worktrees\\" not in gitdir.stdout:
        return None

    try:
        toplevel = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if toplevel.returncode != 0:
        return None
    path = toplevel.stdout.strip()
    if not path:
        return None
    try:
        return str(Path(path).resolve())
    except OSError:
        return path


def _worktree_suffix(toplevel_path: str) -> str:
    """Return the 8-char sha256 hex suffix for a worktree toplevel path."""

    resolved = str(Path(toplevel_path).resolve()) if toplevel_path else ""
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:8]


def _cwd_hash_id(cwd: str | Path) -> str:
    """Return the ``cwd-<12hex>`` fallback id."""

    resolved = str(Path(cwd).resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]
    return f"cwd-{digest}"


def _compose(slug: str, user: str, worktree: str | None = None) -> str:
    """Combine a slug, user, and optional worktree hash into a tenant id."""

    slug = _apply_length_cap(slug)
    base = f"{slug}@{user}"
    if worktree:
        base = f"{base}@{worktree}"
    if len(base) > MAX_TENANT_LEN:
        # Final guard — trim slug further if user or worktree suffix pushed
        # us over. Keep user/worktree intact since they carry identity.
        overflow = len(base) - MAX_TENANT_LEN
        trimmed_slug = slug[: max(1, len(slug) - overflow)].rstrip("-._") or "x"
        base = f"{trimmed_slug}@{user}"
        if worktree:
            base = f"{base}@{worktree}"
    return base


def _raise_ci_guard() -> None:
    """Raise the CI tenant-required DiagnosticError."""

    error_cls = _lazy_diagnostic_error()
    raise error_cls(
        code="TENANT_REQUIRED_IN_CI",
        terminal=True,
        diagnose_cmd='env | grep -E "CI|GITHUB_ACTIONS"',
        next_steps=[
            (
                "Set tenant explicitly",
                "export CODEPROBE_TENANT=ci-$(git rev-parse --short HEAD)",
            ),
        ],
        message=(
            "Tenant must be set explicitly in CI — refusing to auto-derive. "
            "Set CODEPROBE_TENANT or pass --tenant."
        ),
    )


def derive_tenant(
    cwd: str | Path,
    env: Mapping[str, str],
    url_override: str | None = None,
    explicit_flag: str | None = None,
) -> tuple[str, str]:
    """Compute a tenant id and return ``(tenant_id, source)``.

    Priority:

    1. ``CODEPROBE_TENANT`` environment variable.
    2. ``explicit_flag`` argument (i.e. ``--tenant`` passed on the CLI).
    3. CI guard — raise :class:`DiagnosticError` when running under CI
       without an explicit tenant.
    4. ``url_override`` (useful for ``mine`` on a cloned URL when cwd is
       a tempdir that has no git remote yet).
    5. ``origin`` remote of the git repo containing ``cwd``. When ``cwd``
       is inside a linked worktree, the id also carries a short
       worktree-path hash so sibling worktrees get isolated state.
    6. ``cwd-<hash>`` fallback — stable per resolved absolute path.

    All returned ids are validated via
    :func:`codeprobe.paths._validate_tenant_id` before return.
    """

    env_value = (env.get("CODEPROBE_TENANT") or "").strip()
    if env_value:
        _validate_tenant_id(env_value)
        return env_value, "env"

    if explicit_flag:
        flag_value = explicit_flag.strip()
        if flag_value:
            _validate_tenant_id(flag_value)
            return flag_value, "flag"

    if (
        env.get("CI") == "true"
        or env.get("GITHUB_ACTIONS") == "true"
    ):
        _raise_ci_guard()

    user = _resolve_user(env)

    if url_override:
        slug = _normalize_slug(url_override)
        tenant = _compose(slug, user)
        _validate_tenant_id(tenant)
        return tenant, "url-override+user"

    remote = _try_git_remote(cwd)
    if remote:
        slug = _normalize_slug(remote)
        worktree_top = _detect_worktree(cwd)
        if worktree_top:
            tenant = _compose(slug, user, worktree=_worktree_suffix(worktree_top))
            _validate_tenant_id(tenant)
            return tenant, "git-remote+user+worktree"
        tenant = _compose(slug, user)
        _validate_tenant_id(tenant)
        return tenant, "git-remote+user"

    tenant = _compose(_cwd_hash_id(cwd), user)
    _validate_tenant_id(tenant)
    return tenant, "cwd-hash+user"
