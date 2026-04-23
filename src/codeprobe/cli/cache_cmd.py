"""``codeprobe cache`` subcommand group — tenant-scoped cache management.

Currently provides ``codeprobe cache purge --tenant <id>`` to remove the
full ~/.codeprobe/state/<id>/ tree for a single tenant.

This module intentionally does NOT register itself in
``src/codeprobe/cli/__init__.py`` — wiring into the top-level CLI happens
in a later work unit. Importers should invoke the :data:`cache` group
directly (e.g. from tests or future wiring code).
"""

from __future__ import annotations

import shutil

import click

from codeprobe.cli._tenant import tenant_option
from codeprobe.paths import assert_tenant_owned, tenant_root

__all__ = ["cache"]


@click.group()
def cache() -> None:
    """Manage codeprobe's local cache directories."""


@cache.command("purge")
@tenant_option()
def purge(tenant_id: str) -> None:
    """Remove the tenant's ~/.codeprobe/state/<id>/ directory.

    Safe to run when the directory does not exist — exits 0 with a notice.
    Omitting ``--tenant`` triggers Click's usage error and a non-zero exit
    (handled automatically by ``required=True`` on the option).
    """
    root = tenant_root(tenant_id)
    # Defense-in-depth: refuse to rmtree anything that isn't actually
    # under the tenant's state root. `tenant_root` already validates the
    # tenant_id, but if a future refactor ever passes a derived path we
    # want a hard fail-closed guard at the write boundary (INV2).
    assert_tenant_owned(root, tenant_id)
    if not root.exists():
        click.echo(f"No cache for tenant {tenant_id!r} at {root} — nothing to purge.")
        return

    # Use shutil.rmtree so subdirectories (per-repo hashes) are cleared too.
    # Any error here should surface loudly — we do not swallow IO failures.
    shutil.rmtree(root)
    click.echo(f"Purged tenant {tenant_id!r} cache at {root}.")
