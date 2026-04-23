"""Shared Click options for sandbox-aware commands.

This module exposes :func:`sandbox_options`, a decorator that adds the
``--allow-mutating-tools`` opt-in flag to a Click command. The flag
defaults to False (read-only sandbox, per INV4). When the decorator is
applied, the resolved value is stored on the Click context under
``ctx.obj["allow_mutating_tools"]`` so downstream helpers can read one
shared toggle without passing an extra kwarg through every call site.

The decorator is intentionally NOT wired into the top-level CLI group in
this work unit. Consumers opt in by applying ``@sandbox_options`` to
subcommands that should expose the flag.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

import click

F = TypeVar("F", bound=Callable[..., Any])


def sandbox_options(f: F) -> F:
    """Attach ``--allow-mutating-tools`` to a Click command.

    Usage::

        @click.command()
        @sandbox_options
        def my_cmd() -> None:
            ctx = click.get_current_context()
            if ctx.obj["allow_mutating_tools"]:
                ...
    """

    @click.option(
        "--allow-mutating-tools",
        "allow_mutating_tools",
        is_flag=True,
        default=False,
        help=(
            "Opt in to write-capable agent tools (Write/Bash/Edit). "
            "By default mutating tools run inside a read-only sandbox — "
            "see docs on INV4."
        ),
    )
    @click.pass_context
    @wraps(f)
    def wrapper(
        ctx: click.Context,
        *args: Any,
        allow_mutating_tools: bool,
        **kwargs: Any,
    ) -> Any:
        ctx.ensure_object(dict)
        ctx.obj["allow_mutating_tools"] = allow_mutating_tools
        return f(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


__all__ = ["sandbox_options"]
