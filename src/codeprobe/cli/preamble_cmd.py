"""codeprobe preambles — list and inspect available preamble blocks."""

from __future__ import annotations

import re
from pathlib import Path

import click

from codeprobe.preambles import get_builtin, list_builtins

_TEMPLATE_VAR_RE = re.compile(r"\{\{(\w+)\}\}")

_USER_DIR = Path.home() / ".codeprobe" / "preambles"
_PROJECT_DIR = Path(".codeprobe") / "preambles"


def _extract_vars(template: str) -> list[str]:
    """Extract sorted unique {{var}} names from a template string."""
    return sorted(set(_TEMPLATE_VAR_RE.findall(template)))


def _scan_dir(directory: Path) -> list[str]:
    """Return sorted preamble names (stems) from a directory of .md files."""
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.md"))


@click.group()
def preambles() -> None:
    """Manage preamble instruction blocks."""


@preambles.command("list")
def list_cmd() -> None:
    """List available preambles at each search path level.

    Shows built-in, user-level, and project-level preambles with their
    template variables.
    """
    found_any = False

    # Built-in preambles
    builtin_names = list_builtins()
    if builtin_names:
        found_any = True
        click.echo("Built-in preambles:")
        for name in builtin_names:
            block = get_builtin(name)
            variables = _extract_vars(block.template)
            var_str = ", ".join(f"{{{{{v}}}}}" for v in variables) if variables else ""
            desc = block.description
            line = f"  {name}"
            if desc:
                line += f"  — {desc}"
            if var_str:
                line += f"  [{var_str}]"
            click.echo(line)

    # User-level preambles
    user_names = _scan_dir(_USER_DIR)
    if user_names:
        found_any = True
        click.echo()
        click.echo(f"User preambles ({_USER_DIR}):")
        for name in user_names:
            path = _USER_DIR / f"{name}.md"
            template = path.read_text(encoding="utf-8").strip()
            variables = _extract_vars(template)
            var_str = ", ".join(f"{{{{{v}}}}}" for v in variables) if variables else ""
            line = f"  {name}"
            if var_str:
                line += f"  [{var_str}]"
            click.echo(line)

    # Project-level preambles
    project_dir = Path.cwd() / ".codeprobe" / "preambles"
    project_names = _scan_dir(project_dir)
    if project_names:
        found_any = True
        click.echo()
        click.echo(f"Project preambles ({project_dir}):")
        for name in project_names:
            path = project_dir / f"{name}.md"
            template = path.read_text(encoding="utf-8").strip()
            variables = _extract_vars(template)
            var_str = ", ".join(f"{{{{{v}}}}}" for v in variables) if variables else ""
            line = f"  {name}"
            if var_str:
                line += f"  [{var_str}]"
            click.echo(line)

    if not found_any:
        click.echo("No preambles found.")
