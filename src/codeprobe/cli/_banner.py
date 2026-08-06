"""ASCII wordmark printed by ``codeprobe skills install``.

Installing the packaged skills is the moment a pip customer turns
codeprobe from a CLI into something their coding agent drives, so the
command gets a banner and a next-step call to action instead of a bare
list of copied paths.

The renderer is a pure function returning a string; the CLI decides
whether to print it (:func:`should_print_banner` — pretty output on a
TTY only, never in the JSON envelope path). Colour is applied with
``click.style`` so a non-colour terminal still gets legible ASCII.

The sparkline above the wordmark is the product metaphor: per-task
scores across a run.
"""

from __future__ import annotations

import click

# Score sparkline — decorative, matches the per-task bars in `codeprobe run`.
_SPARKLINE = "▁▃▂█▅▇▄█"

# "CODEPROBE" in a two-row half-block font.
_WORDMARK_TOP = "█▀▀ █▀█ █▀▄ █▀▀ █▀█ █▀█ █▀█ █▄▄ █▀▀"
_WORDMARK_BOTTOM = "█▄▄ █▄█ █▄▀ ██▄ █▀▀ █▀▄ █▄█ █▄█ ██▄"

_INDENT = "  "


def render_banner(version: str, *, color: bool = True) -> str:
    """Return the banner block for ``version``.

    ``color=False`` yields the same glyphs with no ANSI sequences, which
    is what the tests assert against and what a redirected stream would
    want.
    """

    def _style(text: str, **kwargs: object) -> str:
        return click.style(text, **kwargs) if color else text  # type: ignore[arg-type]

    return "\n".join(
        [
            "",
            _INDENT + _style(_SPARKLINE, fg="bright_cyan"),
            "",
            _INDENT + _style(_WORDMARK_TOP, fg="cyan"),
            _INDENT
            + _style(_WORDMARK_BOTTOM, fg="cyan")
            + "  "
            + _style(version, dim=True),
            "",
        ]
    )


def should_print_banner(*, mode: str, use_rich: bool) -> bool:
    """True only for pretty output on a TTY.

    Keyed off the resolved :class:`~codeprobe.cli._output_mode.OutputMode`
    fields rather than probing ``sys.stdout`` again, so the JSON envelope
    path can never be polluted with decoration and the decision stays
    unit-testable.
    """
    return mode == "pretty" and use_rich
