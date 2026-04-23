"""Tenant-scoped filesystem paths under ~/.codeprobe/.

Implements PRD invariant INV2: every path under ``~/.codeprobe/`` is
namespaced by ``{tenant_id}/{repo_hash}``. Cross-tenant reads/writes must
fail closed — see :func:`assert_tenant_owned`.

The ``repo_hash`` is a deterministic 16-character hex digest derived from
``sha256(f"{remote}:{ref}:{worktree_root}")``. This gives stable, collision-
resistant directory names without leaking remote URLs or absolute paths.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

__all__ = [
    "CrossTenantAccessError",
    "assert_tenant_owned",
    "compute_repo_hash",
    "DEFAULT_TENANT",
    "tenant_root",
    "tenant_state_dir",
]

DEFAULT_TENANT = "local"


class CrossTenantAccessError(Exception):
    """Raised when a path is accessed outside its owning tenant root.

    This is a fail-closed security boundary: callers that receive this
    exception must refuse the operation, never fall back to a permissive
    read/write.
    """


def _codeprobe_root() -> Path:
    """Return ``~/.codeprobe`` (not created).

    Respects the ``CODEPROBE_STATE_ROOT`` environment variable when set so
    tests can redirect state into a tmp_path without touching the real
    user home. When the env var is set, it is treated as the *state* root
    (i.e. ``~/.codeprobe/state`` — see :func:`tenant_root` which joins
    ``state``); the helper therefore returns the parent of the env value
    so existing ``_codeprobe_root()/"state"/tenant`` joins still resolve
    correctly.
    """
    env_root = os.environ.get("CODEPROBE_STATE_ROOT")
    if env_root:
        # CODEPROBE_STATE_ROOT is the *state* directory itself.
        return Path(env_root).parent if Path(env_root).name else Path(env_root)
    return Path.home() / ".codeprobe"


def _validate_tenant_id(tenant_id: str) -> None:
    """Reject tenant ids that could escape the state root.

    Empty, whitespace-only, or path-separator-bearing ids are rejected
    before they can be joined into a ``Path``. This prevents ``../`` or
    absolute-path escapes via the tenant id itself.
    """
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id must be a non-empty string")
    if "/" in tenant_id or "\\" in tenant_id or tenant_id in {".", ".."}:
        raise ValueError(
            f"tenant_id contains forbidden path characters: {tenant_id!r}"
        )
    if os.sep in tenant_id or (os.altsep and os.altsep in tenant_id):
        raise ValueError(
            f"tenant_id contains forbidden path separator: {tenant_id!r}"
        )


def tenant_root(tenant_id: str) -> Path:
    """Return the root state directory for a tenant.

    Form: ``~/.codeprobe/state/<tenant_id>``. Does not create the directory.
    """
    _validate_tenant_id(tenant_id)
    return _codeprobe_root() / "state" / tenant_id


def compute_repo_hash(
    repo_remote_url: str, repo_ref: str, worktree_root: str
) -> str:
    """Return the 16-char hex sha256 digest identifying a (remote, ref, worktree) triple.

    Deterministic across runs: identical inputs always produce the same
    digest. Inputs are joined with ``:`` as the separator — consistent with
    the PRD specification.
    """
    payload = f"{repo_remote_url}:{repo_ref}:{worktree_root}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def tenant_state_dir(
    tenant_id: str,
    repo_remote_url: str = "",
    repo_ref: str = "",
    worktree_root: str = "",
    *,
    repo_hash: str | None = None,
) -> Path:
    """Return the tenant- and repo-scoped state directory.

    Form: ``~/.codeprobe/state/<tenant_id>/<repo_hash>``. Deterministic:
    identical arguments always produce the same path. Does not create the
    directory — callers decide whether/when to materialize it.

    Callers that already have a precomputed ``repo_hash`` may pass it via
    the keyword-only argument and skip the remote/ref/worktree triple.
    """
    root = tenant_root(tenant_id)
    if repo_hash is not None:
        digest = repo_hash
    else:
        digest = compute_repo_hash(repo_remote_url, repo_ref, worktree_root)
    return root / digest


def _safe_resolve(path: Path) -> Path:
    """Resolve ``path`` without requiring it to exist.

    Uses ``Path.resolve(strict=False)`` so symlinks are still followed for
    any component that does exist, but non-existent tail components are
    simply appended. This matches the semantics we want for fail-closed
    ownership checks on paths that may not yet be materialized.
    """
    return path.resolve(strict=False)


def assert_tenant_owned(path: Path, tenant_id: str) -> None:
    """Raise :class:`CrossTenantAccessError` if *path* is not under the tenant root.

    Both *path* and the tenant root are resolved before comparison so that
    symlinks cannot be used to escape the tenant boundary. Callers that
    receive the exception must refuse the operation outright — never
    silently fall back to a permissive read or write.
    """
    root = _safe_resolve(tenant_root(tenant_id))
    resolved = _safe_resolve(Path(path))
    if not resolved.is_relative_to(root):
        raise CrossTenantAccessError(
            f"Path {resolved} is not owned by tenant {tenant_id!r} "
            f"(expected under {root})"
        )
