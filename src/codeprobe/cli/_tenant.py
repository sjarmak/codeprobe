"""Shared ``--tenant`` option decorator for codeprobe CLI subcommands.

Every subcommand that touches tenant-scoped state must require a
``--tenant`` flag. Using a single decorator keeps the spelling, help text,
and "required" contract consistent across the CLI surface.

Usage::

    from codeprobe.cli._tenant import tenant_option

    @click.command()
    @tenant_option()
    def my_cmd(tenant_id: str) -> None:
        ...
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import click

__all__ = ["tenant_option"]

F = TypeVar("F", bound=Callable[..., object])


def tenant_option() -> Callable[[F], F]:
    """Return a decorator that attaches the ``--tenant`` option to a command.

    The option is marked ``required=True`` so Click exits non-zero with a
    usage error when it is omitted. The value is exposed to the decorated
    function as the ``tenant_id`` keyword argument and also written into
    ``ctx.obj["tenant_id"]`` for downstream helpers via a Click callback.
    """

    def _store_on_context(
        ctx: click.Context, param: click.Parameter, value: str
    ) -> str:
        # Propagate into the Click context object so nested helpers can
        # read the tenant without threading it through every function call.
        ctx.ensure_object(dict)
        ctx.obj["tenant_id"] = value
        return value

    return click.option(
        "--tenant",
        "tenant_id",
        required=True,
        metavar="ID",
        help="Tenant identifier. Namespaces all state under ~/.codeprobe/state/<id>/.",
        callback=_store_on_context,
    )
