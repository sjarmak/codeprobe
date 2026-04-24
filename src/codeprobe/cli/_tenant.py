"""Shared ``--tenant`` option decorator for codeprobe CLI subcommands.

Every subcommand that touches tenant-scoped state takes a ``--tenant``
flag. Using a single decorator keeps the spelling, help text, and
required/optional contract consistent across the CLI surface.

Two usage patterns are supported:

1. **Required flag** — the caller must spell ``--tenant <id>`` explicitly
   on the command line. Used by ``cache purge`` where acting on the wrong
   tenant would be destructive::

       @click.command()
       @tenant_option()
       def purge(tenant_id: str) -> None:
           ...

2. **Optional flag with derivation** — the caller may omit ``--tenant``,
   in which case the effective tenant is computed from the cwd / env via
   :func:`codeprobe.tenant.derive_tenant`. Used by ``mine``, ``run``, and
   ``snapshot create``::

       @click.command()
       @tenant_option(required=False)
       @click.pass_context
       def mine(ctx, tenant_id: str | None, ...) -> None:
           tenant_id_resolved, tenant_source = resolve_tenant(
               ctx, tenant_id, cwd=Path(path), url_override=None,
           )
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypeVar

import click

__all__ = ["tenant_option", "resolve_tenant"]

F = TypeVar("F", bound=Callable[..., object])


def tenant_option(required: bool = True) -> Callable[[F], F]:
    """Return a decorator that attaches the ``--tenant`` option to a command.

    Parameters
    ----------
    required:
        When ``True`` (default), Click exits non-zero with a usage error
        if the flag is omitted. Preserves backward compatibility with
        call sites that hard-require an explicit tenant (e.g.
        ``cache purge``).

        When ``False``, the option becomes optional — callers are expected
        to pair it with :func:`resolve_tenant`, which falls back to
        :func:`codeprobe.tenant.derive_tenant` when the flag is unset.

    The value is exposed to the decorated function as the ``tenant_id``
    keyword argument and, when provided, written into
    ``ctx.obj["tenant_id"]`` for downstream helpers via a Click callback.
    """

    def _store_on_context(
        ctx: click.Context, param: click.Parameter, value: str | None
    ) -> str | None:
        # Propagate into the Click context object so nested helpers can
        # read the tenant without threading it through every function call.
        ctx.ensure_object(dict)
        if value is not None:
            ctx.obj["tenant_id"] = value
        return value

    # Only pass ``default=None`` when the option is optional; Click
    # treats an explicit ``default=None`` on a required option as a
    # "provided value" and suppresses its own "Missing option" usage
    # error. Omitting ``default`` entirely restores the required-flag
    # behaviour without breaking callers that still rely on it.
    option_kwargs: dict = {
        "required": required,
        "metavar": "ID",
        "help": (
            "Tenant identifier. Namespaces all state under "
            "~/.codeprobe/state/<id>/. When omitted, the tenant is derived "
            "from the cwd's git remote + user (see codeprobe.tenant)."
        ),
        "callback": _store_on_context,
    }
    if not required:
        option_kwargs["default"] = None

    return click.option(
        "--tenant",
        "tenant_id",
        **option_kwargs,
    )


def resolve_tenant(
    ctx: click.Context | None,
    explicit: str | None,
    cwd: str | Path,
    url_override: str | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Resolve the effective tenant id and its derivation source.

    Delegates to :func:`codeprobe.tenant.derive_tenant` for the actual
    priority chain (``CODEPROBE_TENANT`` > ``--tenant`` > CI guard >
    ``url_override`` > git remote [+worktree] > cwd hash). This helper
    does *not* duplicate that logic; it just bundles the click-side
    plumbing (context stash, os.environ default) into a single call site
    for CLI commands.

    When ``explicit`` is a truthy string, ``derive_tenant`` will return
    ``(explicit, 'flag')`` without consulting git or the filesystem. The
    CI guard fires only when both ``CODEPROBE_TENANT`` and ``explicit``
    are unset *and* a CI env var is present — so passing
    ``--tenant my-ci`` in CI succeeds.

    Returns
    -------
    (tenant_id, tenant_source): tuple[str, str]
        ``tenant_source`` is one of ``env``, ``flag``,
        ``url-override+user``, ``git-remote+user``,
        ``git-remote+user+worktree``, ``cwd-hash+user``.
    """

    # Import lazily so the foundation tenant module can be swapped /
    # tested without hard-wiring an import cycle at module load time.
    import os

    from codeprobe.tenant import derive_tenant

    effective_env = env if env is not None else os.environ
    tenant_id, tenant_source = derive_tenant(
        cwd=cwd,
        env=effective_env,
        url_override=url_override,
        explicit_flag=explicit,
    )

    # Stash on the click context so error handlers / future helpers can
    # read the resolved tenant without recomputing.
    if ctx is not None:
        ctx.ensure_object(dict)
        ctx.obj["tenant_id"] = tenant_id
        ctx.obj["tenant_source"] = tenant_source

    return tenant_id, tenant_source
