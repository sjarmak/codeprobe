"""``codeprobe purge`` — retention lever for cleartext run artifacts.

Agent transcripts (``agent_output.txt`` / ``agent_error.txt``) and the trace
database (``runs/trace.db``) are written in cleartext with secret-token /
auth-pattern redaction only, so whatever proprietary source the agent printed
is on disk verbatim (see README "Data at rest & retention"). This command
enumerates those artifacts under ``<PATH>/.codeprobe/`` plus stale
``codeprobe-mcp-*.json`` files in the system temp directory, prints them with
sizes (dry run, the default), and deletes them with ``--yes``.

Safety posture (defense-in-depth, mirroring ``cache_cmd``):

* Every deletion target must resolve under ``<PATH>/.codeprobe/`` or be a
  ``codeprobe-mcp-*.json`` file directly inside the temp directory. Any
  candidate that fails the guard — including a candidate tree containing a
  symlink that escapes ``.codeprobe/`` — aborts the purge BEFORE anything is
  deleted (fail closed, ``PURGE_REFUSED``).
* Customer source is never touched and no git command is ever invoked
  against the checkout.

The command is attached to the root CLI from
``src/codeprobe/cli/__init__.py`` (``main.add_command(purge)``).
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import click

from codeprobe.cli.errors import DiagnosticError, PrescriptiveError

__all__ = ["purge"]

_DISCLOSURE = "These files contain your source code and agent transcripts in cleartext."
_MCP_TEMPFILE_PATTERN = "codeprobe-mcp-*.json"
_SECONDS_PER_DAY = 86_400.0

_EPILOG = (
    "Disclosure: run artifacts are stored in cleartext. Agent transcripts "
    "(agent_output.txt / agent_error.txt) and runs/trace.db receive "
    "secret-token and auth-pattern redaction only, so any proprietary source "
    "the agent printed is on disk verbatim. By default this command is a dry "
    "run that lists those artifacts; pass --yes to delete them. Deletion "
    "only ever targets <PATH>/.codeprobe/ plus stale codeprobe-mcp-*.json "
    "files in the system temp directory. Customer source is never touched "
    "and git is never invoked."
)


@dataclass(frozen=True)
class _Candidate:
    """One deletion candidate: a directory tree or a single temp file."""

    path: Path
    size_bytes: int
    newest_mtime: float
    is_dir: bool


def _experiment_dirs(codeprobe_dir: Path) -> list[Path]:
    """Enumerate experiment dirs, mirroring ``run_cmd``'s resolution.

    ``.codeprobe/experiment.json`` marks ``.codeprobe`` itself as the
    experiment dir; otherwise every direct subdirectory holding an
    ``experiment.json`` is one.
    """
    if not codeprobe_dir.is_dir():
        return []
    if (codeprobe_dir / "experiment.json").is_file():
        return [codeprobe_dir]
    return sorted(
        d for d in codeprobe_dir.iterdir() if d.is_dir() and (d / "experiment.json").is_file()
    )


def _tree_stats(root: Path) -> tuple[int, float]:
    """Total file size and newest mtime across *root* (symlinks not followed)."""
    total = 0
    newest = root.lstat().st_mtime
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        newest = max(newest, Path(dirpath).lstat().st_mtime)
        for name in filenames:
            st = (Path(dirpath) / name).lstat()
            total += st.st_size
            newest = max(newest, st.st_mtime)
    return total, newest


def _escaping_path(target: Path, boundary: Path) -> Path | None:
    """Return the first path under *target* that resolves outside *boundary*.

    ``None`` means *target* and everything inside it stays within the
    boundary and the tree is safe to delete. Symlinks are never followed
    during the walk; each one's resolution is checked instead, so a link
    pointing at customer source flags the whole tree.
    """
    if target.is_symlink() or not target.resolve().is_relative_to(boundary):
        return target
    for dirpath, dirnames, filenames in os.walk(target, followlinks=False):
        for name in (*dirnames, *filenames):
            entry = Path(dirpath) / name
            if entry.is_symlink() and not entry.resolve().is_relative_to(boundary):
                return entry
    return None


def _human_size(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KB", "MB"):
        if value < 1024.0:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} GB"


@click.command("purge", epilog=_EPILOG)
@click.argument(
    "path",
    default=".",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--older-than",
    "older_than",
    type=click.IntRange(min=0),
    default=None,
    metavar="DAYS",
    help="Only purge artifacts whose newest mtime is more than DAYS days old.",
)
@click.option(
    "--all",
    "purge_all",
    is_flag=True,
    help="Remove whole experiment directories (experiment.json, tasks/), not just runs/ artifacts.",
)
@click.option(
    "--yes",
    is_flag=True,
    help="Actually delete. Without this flag the command is a dry run.",
)
def purge(path: Path, older_than: int | None, purge_all: bool, yes: bool) -> None:
    """List or delete cleartext run artifacts under PATH/.codeprobe/.

    Dry run by default: prints every candidate with its size and exits
    without deleting anything. Candidates are the runs/ trees (agent
    transcripts + trace.db) of each experiment under PATH/.codeprobe/ —
    whole experiment directories with --all — plus stale
    codeprobe-mcp-*.json files in the system temp directory.
    """
    root = path.resolve()
    codeprobe_dir = root / ".codeprobe"
    # Resolving the containment boundary AFTER following .codeprobe made the
    # boundary move with the link, so a symlinked .codeprobe let every
    # deletion pass the escape check and destroyed the link target while
    # printing the innocuous in-project path. Symlinking .codeprobe onto a
    # data volume is ordinary practice, so refuse rather than guess.
    if codeprobe_dir.is_symlink():
        raise PrescriptiveError(
            code="PURGE_SYMLINKED_ROOT",
            message=(
                f"{codeprobe_dir} is a symlink to "
                f"{os.path.realpath(codeprobe_dir)}. Refusing to purge through "
                "it: the deletion boundary would follow the link and the "
                "printed paths would not show what is actually removed. Purge "
                "the link target directly instead."
            ),
            next_try_flag="",
            next_try_value="",
            detail={"target": os.path.realpath(codeprobe_dir)},
        )
    boundary = codeprobe_dir.resolve()
    cutoff = time.time() - older_than * _SECONDS_PER_DAY if older_than is not None else None

    candidates: list[_Candidate] = []
    for exp_dir in _experiment_dirs(codeprobe_dir):
        target = exp_dir if purge_all else exp_dir / "runs"
        if not target.is_dir():
            continue
        size, newest = _tree_stats(target)
        if cutoff is not None and newest >= cutoff:
            continue
        candidates.append(_Candidate(path=target, size_bytes=size, newest_mtime=newest, is_dir=True))

    tempdir = Path(tempfile.gettempdir())
    for entry in sorted(tempdir.glob(_MCP_TEMPFILE_PATTERN)):
        if entry.is_symlink() or not entry.is_file():
            continue
        st = entry.lstat()
        if cutoff is not None and st.st_mtime >= cutoff:
            continue
        candidates.append(
            _Candidate(path=entry, size_bytes=st.st_size, newest_mtime=st.st_mtime, is_dir=False)
        )

    if not candidates:
        click.echo("Nothing to purge.")
        return

    total = sum(c.size_bytes for c in candidates)
    click.echo(_DISCLOSURE)
    for cand in candidates:
        click.echo(f"  {cand.path}  ({_human_size(cand.size_bytes)})")

    if not yes:
        click.echo(
            f"Dry run: nothing deleted. Re-run with --yes to delete "
            f"{len(candidates)} item(s), {_human_size(total)} total."
        )
        return

    # Fail-closed guard: validate EVERY candidate before deleting anything,
    # so one escaping symlink aborts the whole purge with nothing removed.
    refused: list[tuple[Path, Path]] = []
    for cand in candidates:
        if cand.is_dir:
            offender = _escaping_path(cand.path, boundary)
            if offender is not None:
                refused.append((cand.path, offender))
        else:
            in_tempdir = (
                cand.path.parent == tempdir
                and fnmatch.fnmatch(cand.path.name, _MCP_TEMPFILE_PATTERN)
                and not cand.path.is_symlink()
            )
            if not in_tempdir:
                refused.append((cand.path, cand.path))

    if refused:
        listing = "; ".join(f"{candidate} (via {offender})" for candidate, offender in refused)
        raise DiagnosticError(
            code="PURGE_REFUSED",
            message=(
                f"Refusing to delete {len(refused)} candidate(s) that resolve outside "
                f"{boundary}: {listing}. Nothing was deleted."
            ),
            diagnose_cmd=f"ls -la {refused[0][1]}",
            next_steps=[
                ("Inspect the offending path", f"ls -la {refused[0][1]}"),
                ("Remove the escaping symlink, then re-run", f"codeprobe purge {path} --yes"),
            ],
            detail={"refused": [str(candidate) for candidate, _ in refused]},
        )

    for cand in candidates:
        if cand.is_dir:
            shutil.rmtree(cand.path)
        else:
            cand.path.unlink()
        click.echo(f"Deleted {cand.path}")
    click.echo(f"Purged {len(candidates)} item(s), {_human_size(total)} reclaimed.")
