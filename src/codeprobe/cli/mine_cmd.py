"""codeprobe mine — extract eval tasks from repo history."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import is_dataclass, replace
from pathlib import Path

import click

from codeprobe.mining.extractor import MineResult
from codeprobe.mining.org_scale import OrgScaleMineResult
from codeprobe.mining.org_scale_families import TaskFamily
from codeprobe.models.task import Task

# ---------------------------------------------------------------------------
# URL → local clone
# ---------------------------------------------------------------------------

_GIT_URL_PATTERN = re.compile(
    r"^(?:https?://|git@)"  # https:// or git@
    r"|^[\w.-]+/[\w.-]+$"  # owner/repo shorthand
)

# Schemes we recognize as potentially-cloneable.  Anything else is rejected
# with a "not a valid git URL" message before we reach git itself.
_ACCEPTED_GIT_URL_SCHEMES = frozenset(
    {"http", "https", "git", "ssh", "git+http", "git+https", "git+ssh"}
)


def _is_git_url(path_or_url: str) -> bool:
    """Return True if the argument looks like a git URL or owner/repo shorthand."""
    return bool(_GIT_URL_PATTERN.match(path_or_url))


def _normalize_url(url: str) -> str:
    """Expand owner/repo shorthand to a full GitHub URL."""
    if "/" in url and not url.startswith(("https://", "http://", "git@")):
        return f"https://github.com/{url}.git"
    return url


def _validate_git_url_shape(url: str) -> None:
    """Reject obvious non-git URLs before we invoke ``git clone``.

    Catches two common first-use mistakes:
      * Passing a URL with a non-git scheme (``ftp://``, ``file://`` etc.)
      * Passing a host-only URL (``https://example.com`` with no repo path)

    ``git@host:owner/repo`` shorthand is accepted unchanged — it is not a
    URL per RFC 3986 and urllib.parse cannot validate it.

    Raises ``click.UsageError`` with an actionable message. SSRF filtering
    is handled separately in :func:`_validate_clone_url`.
    """
    from urllib.parse import urlparse

    if url.startswith("git@"):
        return  # SCP-like shorthand, not a URL — let git handle it

    parsed = urlparse(url)
    if not parsed.scheme:
        return  # No scheme → treated as a local path upstream
    if parsed.scheme not in _ACCEPTED_GIT_URL_SCHEMES:
        raise click.UsageError(
            f"URL {url!r} is not a valid git URL: "
            f"scheme {parsed.scheme!r} is not one of "
            f"{sorted(_ACCEPTED_GIT_URL_SCHEMES)}. "
            "Pass an https/ssh git URL or a local path."
        )
    # urlparse keeps the leading slash in .path, so a bare host has path=''
    # and 'owner/repo' shorthand never reaches this function (see _is_git_url).
    if not parsed.netloc:
        raise click.UsageError(
            f"URL {url!r} is not a valid git URL: missing host."
        )
    if parsed.path in ("", "/"):
        raise click.UsageError(
            f"URL {url!r} is not a valid git URL: "
            "missing repository path (expected e.g. "
            "https://host.example/owner/repo.git)."
        )


def _validate_clone_url(url: str) -> None:
    """Reject URLs targeting private/link-local/loopback addresses (SSRF guard)."""
    import ipaddress
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname or ""
    try:
        addr = ipaddress.ip_address(host)
        if not addr.is_global:
            raise click.UsageError(
                f"Refusing to clone from private/link-local address: {host}"
            )
    except ValueError:
        pass  # hostname, not IP literal — allow


def _clone_repo(url: str) -> Path:
    """Shallow-clone a repo into a temp directory. Returns the clone path.

    Uses ``--filter=blob:none`` for a fast treeless clone. The temp directory
    persists until the process exits (the user sees the path in output).
    """
    url = _normalize_url(url)
    _validate_git_url_shape(url)
    _validate_clone_url(url)
    # Derive a directory name from the URL
    repo_name = url.rstrip("/").rstrip(".git").rsplit("/", 1)[-1]
    clone_dir = Path(tempfile.mkdtemp(prefix=f"codeprobe-{repo_name}-"))

    click.echo(f"Cloning {url} → {clone_dir} ...", err=True)
    try:
        subprocess.run(
            ["git", "clone", "--filter=blob:none", url, str(clone_dir)],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(clone_dir, ignore_errors=True)
        stderr = (exc.stderr or "").strip()
        raise click.UsageError(
            f"Could not clone {url}.\n"
            f"  git error: {stderr or 'unknown failure'}\n"
            "  Check that the URL is correct, the repository is public, "
            "or that you have access (try `git clone` manually to verify)."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(clone_dir, ignore_errors=True)
        raise click.UsageError(
            f"Clone of {url} timed out after 120s. "
            "The repository may be large or the network slow; "
            "try cloning manually and pass the local path instead."
        ) from exc

    click.echo(f"Cloned to {clone_dir}", err=True)
    return clone_dir


# ---------------------------------------------------------------------------
# Interactive workflow (mirrors mine-tasks skill phases 0–6)
# ---------------------------------------------------------------------------

# Eval goals are keyed directly by the --goal flag value. Each entry carries
# the display name, defaults for min_files/bias/task_type, and an *extras* dict
# of flag overrides that are applied in resolve_effective_config() only when
# the corresponding flag is still at its Click default (i.e. neither a profile
# nor an explicit CLI flag has touched it).
#
# Known limitation: because flags like --enrich / --org-scale / --mcp-families
# are positive-only (no --no-enrich), a goal that turns one of these on cannot
# be overridden back to False from the CLI. Users who need that should choose
# a different goal or save a custom profile.
_EVAL_GOALS: dict[str, dict] = {
    "quality": {
        "name": "Code quality comparison",
        "bias": "mixed",
        "task_type": "sdlc_code_change",
        "extras": {"enrich": True, "min_files": 2},
    },
    "navigation": {
        "name": "Codebase navigation",
        "bias": "mixed",
        "task_type": "architecture_comprehension",
        "extras": {},
    },
    "mcp": {
        "name": "MCP / tool benefit",
        "bias": "hard",
        "task_type": "mcp_tool_usage",
        "extras": {
            "enrich": True,
            "org_scale": True,
            "mcp_families": True,
            "count": 8,
            "min_files": 6,
        },
    },
    "general": {
        "name": "General benchmarking",
        "bias": "balanced",
        "task_type": "mixed",
        "extras": {},
    },
}

# Map the interactive prompt's numeric choices back to goal names.
_NUMERIC_GOAL_KEYS: dict[str, str] = {
    "1": "quality",
    "2": "navigation",
    "3": "mcp",
    "4": "general",
}

_COUNT_PRESETS = {
    "1": ("Quick look (3-5)", 5),
    "2": ("Standard (5-10)", 8),
    "3": ("Thorough (10-20)", 15),
}

_SOURCE_OPTIONS = {
    "1": ("Auto-detect", "auto"),
    "2": ("GitHub", "github"),
    "3": ("GitLab", "gitlab"),
    "4": ("Bitbucket", "bitbucket"),
    "5": ("Azure DevOps", "azure"),
    "6": ("Gitea/Forgejo", "gitea"),
    "7": ("Local only", "local"),
}


def _is_interactive() -> bool:
    """Return True if stdin is a TTY (interactive terminal)."""
    return sys.stdin.isatty()


def _ask_eval_goal() -> tuple[str, int, str, str]:
    """Phase 0: Ask what the user is trying to learn.

    Returns ``(goal_name, min_files, bias, task_type)``.
    """
    click.echo()
    click.echo("What are you trying to learn?")
    click.echo(
        "  [1] Code quality comparison — SDLC tasks to compare code change quality"
    )
    click.echo(
        "  [2] Codebase navigation — comprehension tasks for architecture understanding"
    )
    click.echo(
        "  [3] MCP / tool benefit — harder tasks requiring cross-file navigation"
    )
    click.echo("  [4] General benchmarking — balanced mix")
    click.echo()

    choice = click.prompt("Select goal", default="4", show_default=True)
    goal_key = _NUMERIC_GOAL_KEYS.get(choice, "general")
    goal = _EVAL_GOALS[goal_key]
    click.echo(f"  → {goal['name']}")
    return (
        goal["name"],
        goal["extras"].get("min_files", 0),
        goal["bias"],
        goal["task_type"],
    )


def _ask_task_count() -> int:
    """Phase 1: Ask how many tasks to mine."""
    click.echo()
    click.echo("How many tasks?")
    click.echo("  [1] Quick look (5) — fast results, good for first experiment")
    click.echo("  [2] Standard (8) — good coverage, enough to see patterns")
    click.echo("  [3] Thorough (15) — statistical confidence for real decisions")
    click.echo()

    choice = click.prompt("Select count", default="2", show_default=True)
    _, count = _COUNT_PRESETS.get(choice, _COUNT_PRESETS["2"])
    return count


def _ask_source() -> str:
    """Phase 1: Ask which git host."""
    click.echo()
    click.echo("Git host?")
    click.echo("  [1] Auto-detect")
    click.echo("  [2] GitHub")
    click.echo("  [3] GitLab")
    click.echo("  [4] Bitbucket")
    click.echo("  [5] Azure DevOps")
    click.echo("  [6] Gitea/Forgejo")
    click.echo("  [7] Local only")
    click.echo()

    choice = click.prompt("Select host", default="1", show_default=True)
    _, source = _SOURCE_OPTIONS.get(choice, _SOURCE_OPTIONS["1"])
    return source


def _show_preflight(
    repo_path: Path,
    goal_name: str,
    count: int,
    source: str,
    min_files: int,
    bias: str,
    subsystems: tuple[str, ...],
) -> bool:
    """Phase 2: Show pre-flight summary and confirm."""
    click.echo()
    click.echo("=" * 50)
    click.echo("Mining plan")
    click.echo("=" * 50)
    click.echo(f"  Goal:       {goal_name}")
    click.echo(f"  Repo:       {repo_path}")
    click.echo(f"  Tasks:      {count}")
    click.echo(f"  Source:      {source}")
    click.echo(f"  Min files:   {min_files} (biasing toward {bias} tasks)")
    if subsystems:
        click.echo(f"  Subsystems: {', '.join(subsystems)}")
    click.echo("=" * 50)
    click.echo()

    return click.confirm("Proceed?", default=True)


def _quality_review(
    tasks: list[Task],
    goal_name: str,
    bias: str,
) -> list[str]:
    """Phase 4: Review mined tasks for quality issues.

    Returns a list of warning strings.
    """
    from collections import Counter

    warnings: list[str] = []

    if not tasks:
        return warnings

    # Difficulty distribution
    difficulties = Counter(t.metadata.difficulty for t in tasks)
    total = len(tasks)

    if bias == "hard":
        easy_pct = difficulties.get("easy", 0) / total
        if easy_pct > 0.5:
            warnings.append(
                f"Difficulty mismatch: {easy_pct:.0%} easy tasks, but goal needs "
                "harder tasks. Try re-mining with higher --min-files."
            )
    elif bias == "mixed":
        if len(difficulties) == 1:
            only = list(difficulties.keys())[0]
            warnings.append(
                f"No difficulty variance: all tasks are '{only}'. "
                "Need a mix to differentiate models/prompts."
            )

    # Instruction quality — check for generic/thin instructions
    thin_count = 0
    for t in tasks:
        desc = t.metadata.description
        # Check if description is mostly template fragments
        if len(desc) < 50 or "reproduce changes from merge" in desc.lower():
            thin_count += 1
    if thin_count > 0:
        warnings.append(
            f"{thin_count}/{total} tasks have thin instructions. "
            "Consider running with --enrich to add problem context via LLM."
        )

    # Test quality — check for generic stubs
    stub_count = sum(1 for t in tasks if t.verification.command == "bash tests/test.sh")
    if stub_count > 0:
        warnings.append(
            f"{stub_count}/{total} tasks use generic test stubs instead of "
            "targeted test commands."
        )

    # Task diversity — check if clustered in one area
    if total >= 3:
        dirs = []
        for t in tasks:
            # Use the first test file path as a proxy for location
            cmd = t.verification.command
            parts = cmd.split()
            if len(parts) >= 2:
                first_path = parts[1] if not parts[1].startswith("-") else ""
                top_dir = first_path.split("/")[0] if "/" in first_path else ""
                if top_dir:
                    dirs.append(top_dir)
        if dirs:
            top_dir_count = Counter(dirs).most_common(1)
            if top_dir_count and top_dir_count[0][1] / total > 0.7:
                warnings.append(
                    f"Low diversity: {top_dir_count[0][1]}/{total} tasks are in "
                    f"'{top_dir_count[0][0]}/'. Consider --subsystem to spread coverage."
                )

    return warnings


def _show_results_table(tasks: list[Task]) -> None:
    """Phase 5: Display mined tasks in a table."""
    click.echo()
    click.echo(f"Mined {len(tasks)} tasks:")
    click.echo()

    # Header
    click.echo(
        f"  {'#':>2}  {'Task ID':<14} {'Difficulty':<12} "
        f"{'Language':<12} {'Quality':>7}"
    )
    click.echo("  " + "-" * 52)

    for i, t in enumerate(tasks, 1):
        click.echo(
            f"  {i:>2}  {t.id:<14} {t.metadata.difficulty:<12} "
            f"{t.metadata.language or 'unknown':<12} "
            f"{t.metadata.quality_score:>6.0%}"
        )
    click.echo()


def _show_next_steps(
    repo_path: Path,
    min_files: int,
    *,
    llm_enriched: bool = False,
    tasks_dir: Path | None = None,
) -> None:
    """Phase 6: Show concrete next-step commands (AC5).

    The first step points at ``codeprobe validate`` so users can verify
    task-directory structure before running a full eval — a cheap,
    offline check that catches malformed outputs early.
    """
    click.echo("Next steps:")
    click.echo()
    step = 1
    if tasks_dir is not None:
        click.echo(f"  {step}. Validate task structure (offline sanity check):")
        click.echo(f"     codeprobe validate {tasks_dir}")
        click.echo()
        step += 1
    if not llm_enriched:
        click.echo(f"  {step}. Review and enrich task instructions (recommended):")
        click.echo(f"     codeprobe mine {repo_path} --enrich")
        click.echo()
        step += 1
    click.echo(f"  {step}. Run the eval:")
    click.echo(f"     codeprobe run {repo_path} --agent claude")
    click.echo()
    step += 1
    click.echo(f"  {step}. Try a different model:")
    click.echo(
        f"     codeprobe run {repo_path} --agent claude --model claude-sonnet-4-6"
    )
    click.echo()
    step += 1
    click.echo(f"  {step}. Set a cost budget:")
    click.echo(f"     codeprobe run {repo_path} --agent claude --max-cost-usd 5.00")
    click.echo()
    if min_files > 0:
        click.echo(f"  {step + 1}. Mine more tasks for better confidence:")
        click.echo(
            f"     codeprobe mine {repo_path} --count 15 --min-files {min_files}"
        )
        click.echo()


# ---------------------------------------------------------------------------
# Subsystem discovery (unchanged)
# ---------------------------------------------------------------------------


def _discover_and_select(
    repo_path: Path,
    source_hint: str,
) -> tuple[str, ...]:
    """List subsystems from merge history and let the user pick.

    Returns selected subsystem prefixes. Falls back to top 3 in
    non-interactive environments.
    """
    from codeprobe.mining import extract_subsystems
    from codeprobe.mining.extractor import list_merged_prs
    from codeprobe.mining.sources import RepoSource, detect_source

    if source_hint != "auto":
        source = RepoSource(
            host=source_hint, owner="", repo=repo_path.name, remote_url=""
        )
    else:
        source = detect_source(repo_path)

    prs = list_merged_prs(source, repo_path, limit=40)
    if not prs:
        click.echo("No merge commits found — cannot discover subsystems.")
        return ()

    subsystem_counts = extract_subsystems(prs, repo_path)
    if not subsystem_counts:
        click.echo("No subsystems detected in merge history.")
        return ()

    # Display the subsystem table
    entries = list(subsystem_counts.items())[:20]
    click.echo()
    click.echo("Subsystems (by merge activity):")
    for i, (prefix, count) in enumerate(entries, 1):
        click.echo(f"  [{i:2d}] {prefix:40s} ({count} merges)")
    click.echo()

    # Non-interactive fallback: pick top 3
    if not sys.stdin.isatty():
        top = tuple(p for p, _ in entries[:3])
        click.echo(f"Non-interactive: auto-selected {', '.join(top)}")
        return top

    raw = click.prompt(
        "Select subsystems (comma-separated numbers, or Enter for top 3)",
        default="",
        show_default=False,
    )

    if not raw.strip():
        return tuple(p for p, _ in entries[:3])

    selected: list[str] = []
    for token in raw.split(","):
        token = token.strip()
        try:
            idx = int(token)
            if 1 <= idx <= len(entries):
                selected.append(entries[idx - 1][0])
            else:
                click.echo(f"  Skipping out-of-range index: {idx}")
        except ValueError:
            # Treat as a literal prefix
            if not token.endswith("/"):
                token += "/"
            selected.append(token)

    if not selected:
        return tuple(p for p, _ in entries[:3])

    return tuple(selected)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


_CURRENT_TASKS_DIR: Path | None = None


def _clear_tasks_dir(repo_path: Path) -> Path:
    """Clear stale tasks and return the tasks directory path.

    Records the path in module state so that the top-level ``run_mine``
    handler can remove a partially-populated directory on Ctrl-C.
    """
    from codeprobe.core.repo_hygiene import ensure_codeprobe_excluded

    ensure_codeprobe_excluded(repo_path)

    tasks_dir = repo_path / ".codeprobe" / "tasks"
    if tasks_dir.exists():
        shutil.rmtree(tasks_dir)
    global _CURRENT_TASKS_DIR
    _CURRENT_TASKS_DIR = tasks_dir
    return tasks_dir


def _record_task_ids_in_experiment(repo_path: Path, task_ids: list[str]) -> None:
    """Update the experiment's task_ids so ``run`` only executes these tasks.

    If exactly one experiment exists under ``<repo>/.codeprobe/``, its
    ``experiment.json`` is updated with the new task ID list.  When zero
    or multiple experiments exist, this is a no-op (the user must scope
    manually via ``--config``).
    """
    from codeprobe.core.experiment import load_experiment, save_experiment
    from codeprobe.models.experiment import Experiment

    codeprobe_dir = repo_path / ".codeprobe"
    if not codeprobe_dir.is_dir():
        return

    candidates = sorted(
        d
        for d in codeprobe_dir.iterdir()
        if d.is_dir() and (d / "experiment.json").is_file()
    )
    if len(candidates) != 1:
        return

    exp_dir = candidates[0]
    experiment = load_experiment(exp_dir)
    updated = Experiment(
        name=experiment.name,
        description=experiment.description,
        configs=experiment.configs,
        tasks_dir=experiment.tasks_dir,
        task_ids=tuple(sorted(task_ids)),
    )
    save_experiment(exp_dir, updated)


def _suggest_path(missing: Path) -> str | None:
    """Return a close sibling filename for *missing*, if any."""
    import difflib

    parent = missing.parent
    try:
        if not parent.is_dir():
            return None
        siblings = [p.name for p in parent.iterdir() if p.is_dir()]
    except OSError:
        return None
    matches = difflib.get_close_matches(missing.name, siblings, n=1, cutoff=0.6)
    if not matches:
        return None
    return str(parent / matches[0])


def _validate_git_repo(repo_path: Path) -> None:
    """Raise click.UsageError if *repo_path* is not a usable git repo.

    Structural check only: the path must be a directory that contains a
    ``.git`` entry. Corruption (empty .git, detached HEAD, etc.) surfaces
    later when mining actually invokes git — tests can mock git without
    installing a real .git database.
    """
    if not repo_path.is_dir():
        raise click.UsageError(
            f"Path is not a directory: {repo_path}. "
            "Pass the path to a local git repository."
        )
    if not (repo_path / ".git").exists():
        raise click.UsageError(
            f"Not a git repository: {repo_path} "
            "(no .git/ directory found). "
            "Initialize with `git init` or pass a different path."
        )


def _looks_like_url(path: str) -> bool:
    """Return True when *path* looks like a URL (any scheme), not a filesystem path.

    Used to route obviously-URL-shaped inputs (e.g. ``ftp://foo``) through
    URL validation so users get a "not a valid git URL" error rather than
    a misleading "Path does not exist" error.
    """
    from urllib.parse import urlparse

    parsed = urlparse(path)
    # A real URL has both a non-empty scheme and netloc. Drive-letter paths
    # on Windows (``C:\foo``) parse with scheme='c' but netloc='', so the
    # netloc check excludes them.
    return bool(parsed.scheme) and bool(parsed.netloc)


def _resolve_repo_path(path: str) -> Path:
    """Resolve a path or URL to a local repo directory.

    Raises ``click.UsageError`` (exit 2) with actionable messages when the
    path does not exist, is not a directory, or is not a git repository.
    """
    if _is_git_url(path):
        return _clone_repo(path)
    # URL-shaped inputs that our git-URL regex rejected (wrong scheme, etc.)
    # are routed through the URL validator so the user gets a URL-appropriate
    # error, not a confusing "Path does not exist".
    if _looks_like_url(path):
        _validate_git_url_shape(path)
        # _validate_git_url_shape will raise; this is a safety net.
        raise click.UsageError(f"URL {path!r} is not a valid git URL.")
    repo_path = Path(path).resolve()
    if not repo_path.exists():
        suggestion = _suggest_path(repo_path)
        hint = f" Did you mean: {suggestion}?" if suggestion else ""
        raise click.UsageError(
            f"Path does not exist: {repo_path}.{hint}"
        )
    _validate_git_repo(repo_path)
    return repo_path


def _interactive_config(
    count: int,
    source: str,
    min_files: int,
    subsystems: tuple[str, ...],
    discover_subsystems: bool,
    repo_path: Path,
) -> tuple[str, int, str, int, str, str, tuple[str, ...], bool]:
    """Run interactive configuration phases 0-2. Returns updated params.

    Returns ``(goal_name, count, source, min_files, bias, task_type, subsystems,
    discover_subsystems)``.
    """
    goal_name, goal_min_files, bias, task_type = _ask_eval_goal()
    if min_files == 0:
        min_files = goal_min_files
    count = _ask_task_count()
    source = _ask_source()
    if not subsystems and not discover_subsystems:
        if click.confirm("\nDiscover and filter by subsystems?", default=False):
            discover_subsystems = True
    return (
        goal_name,
        count,
        source,
        min_files,
        bias,
        task_type,
        subsystems,
        discover_subsystems,
    )


def _was_llm_used(no_llm: bool) -> bool:
    """Check if LLM was available and used for instruction generation."""
    if no_llm:
        return False
    from codeprobe.core.llm import llm_available

    return llm_available()


def _enrich_sdlc_tasks(
    tasks: list[Task],
    mine_result: MineResult,
    no_llm: bool,
    enrich: bool,
) -> list[Task]:
    """Apply LLM instruction generation or legacy enrichment to SDLC tasks."""
    if not no_llm:
        from codeprobe.core.llm import llm_available
        from codeprobe.mining import generate_instructions

        if llm_available():
            click.echo("Generating instructions via LLM...")
            return generate_instructions(
                tasks,
                pr_bodies=mine_result.pr_bodies,
                changed_files_map=mine_result.changed_files_map,
            )
        click.echo(
            "No LLM backend available — using regex fallback for instructions.\n"
            "Install an LLM backend for better quality: "
            "pip install codeprobe[anthropic]"
        )
    elif enrich:
        from codeprobe.mining.extractor import enrich_tasks

        click.echo("Enriching low-quality tasks via LLM...")
        return enrich_tasks(tasks)
    return tasks


import logging as _logging  # noqa: E402

_log = _logging.getLogger(__name__)

# Start time of the current `run_mine` invocation, used to print an elapsed
# time in the end-of-run summary. Set at the top of ``run_mine`` and consumed
# inside ``_finish_mine_output`` / ``_show_org_scale_results``. Module state is
# acceptable here because ``run_mine`` is invoked at most once per process.
_MINE_START_TIME: float | None = None


def _format_elapsed(seconds: float) -> str:
    """Format *seconds* as ``Xm Ys`` for the summary block."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    mins, secs = divmod(int(seconds), 60)
    return f"{mins}m {secs}s"


def _print_summary_block(
    *,
    task_count: int,
    quality_warning_count: int,
    tasks_dir: Path,
    suite_path: Path | None,
    llm_enriched: bool | None = None,
) -> None:
    """Print the structured end-of-run summary block (AC4).

    Called from both single-repo and org-scale completion paths.
    """
    click.echo()
    click.echo("=" * 52)
    click.echo("Mining summary")
    click.echo("=" * 52)
    click.echo(f"  Tasks mined:     {task_count}")
    if quality_warning_count > 0:
        click.echo(f"  Quality gate:    {quality_warning_count} warning(s)")
    else:
        click.echo("  Quality gate:    ok")
    if _MINE_START_TIME is not None:
        elapsed = time.monotonic() - _MINE_START_TIME
        click.echo(f"  Time elapsed:    {_format_elapsed(elapsed)}")
    if llm_enriched is not None:
        click.echo(
            f"  Instructions:    {'LLM-enriched' if llm_enriched else 'regex fallback'}"
        )
    click.echo(f"  Output:          {tasks_dir}")
    if suite_path is not None:
        click.echo(f"  Suite manifest:  {suite_path}")
    click.echo("=" * 52)
    click.echo()


def _cold_start_check(repo_path: Path, source_hint: str) -> bool:
    """Return True when the repo has zero merge commits (cold-start)."""
    from codeprobe.mining.extractor import list_merged_prs
    from codeprobe.mining.sources import RepoSource, detect_source

    if source_hint != "auto":
        source = RepoSource(
            host=source_hint, owner="", repo=repo_path.name, remote_url=""
        )
    else:
        source = detect_source(repo_path)

    return len(list_merged_prs(source, repo_path, limit=1)) == 0


def _comprehension_generator_available() -> bool:
    """Return True when the comprehension generator module is importable."""
    try:
        from codeprobe.mining import comprehension_generator  # noqa: F401

        return True
    except (ImportError, AttributeError):
        return False


def _suitability_warnings(
    task_type: str,
    repo_path: Path,
) -> list[str]:
    """Run a lightweight suitability check for *task_type* against *repo_path*.

    Returns a list of non-blocking warnings. An empty list means "no
    concerns". The check is intentionally cheap: it only inspects file
    counts, language heuristics, and test-infrastructure presence —
    sufficient to flag obvious mismatches (e.g. selecting
    ``org_scale_cross_repo`` on a 5-file demo repo) without slowing down
    the mining pipeline.

    Callers decide whether to prompt the user or proceed; this function
    does no I/O beyond ``Path.glob`` / ``Path.is_dir`` lookups.
    """
    warnings: list[str] = []
    if not repo_path.is_dir():
        return warnings  # handled elsewhere

    # Collect tracked source files (cheap glob — skip VCS + venv detritus).
    source_exts = {
        ".py",
        ".go",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".java",
        ".kt",
        ".rs",
        ".rb",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
    }
    skip_dirs = {".git", "node_modules", "vendor", ".venv", "venv", "dist", "build"}

    source_files: list[Path] = []
    has_tests_dir = False
    for p in repo_path.rglob("*"):
        parts = set(p.parts)
        if parts & skip_dirs:
            continue
        if p.is_file() and p.suffix.lower() in source_exts:
            source_files.append(p)
        if p.is_dir() and p.name in {"tests", "test", "__tests__", "spec"}:
            has_tests_dir = True
        if len(source_files) > 2000:
            break  # cap — we only need rough signals
    file_count = len(source_files)

    if task_type in ("org_scale_cross_repo",) and file_count < 50:
        warnings.append(
            f"task-type=org_scale_cross_repo expects a medium/large codebase "
            f"but {repo_path.name} has only ~{file_count} source files; "
            "org-scale families may produce no hits above their min_hits thresholds."
        )

    if task_type == "sdlc_code_change" and not has_tests_dir:
        warnings.append(
            f"task-type=sdlc_code_change is scored via test scripts, but "
            f"{repo_path.name} has no tests/ directory — generated tasks "
            "will likely rely on weak fallback scoring."
        )

    if task_type == "mcp_tool_usage" and file_count < 100:
        warnings.append(
            f"task-type=mcp_tool_usage benefits from cross-file navigation "
            f"over a medium/large codebase; {repo_path.name} has only "
            f"~{file_count} source files and may not exercise MCP value."
        )

    if task_type == "architecture_comprehension" and file_count < 20:
        warnings.append(
            f"task-type=architecture_comprehension needs enough modules to "
            f"form a coherent architecture; {repo_path.name} has only "
            f"~{file_count} source files."
        )

    return warnings


def _run_suitability_check(
    task_type: str,
    repo_path: Path,
    interactive: bool,
) -> bool:
    """Print any suitability warnings and, when interactive, prompt the user.

    Returns True to proceed, False when the user declines.
    """
    warnings = _suitability_warnings(task_type, repo_path)
    if not warnings:
        return True

    click.echo()
    click.echo(f"Suitability warnings for task-type={task_type}:")
    for w in warnings:
        click.echo(f"  ! {w}")
    click.echo()

    if not interactive:
        return True  # non-interactive: log and proceed
    return click.confirm(
        "This task type may not produce useful tasks for this repo. Continue?",
        default=False,
    )


def _resolve_task_type(
    task_type: str,
    repo_path: Path,
    source_hint: str,
) -> str:
    """Apply cold-start and comprehension-availability fallbacks.

    Cold-start: when the repo has 0 merge commits, navigation and general
    goals fall back to ``micro_probe``.

    Comprehension fallback: when ``architecture_comprehension`` is selected
    but the comprehension generator is not yet available, fall back to
    ``micro_probe``.

    Returns the (possibly adjusted) *task_type*.
    """
    is_cold_start = _cold_start_check(repo_path, source_hint)

    if is_cold_start and task_type in ("architecture_comprehension", "mixed"):
        _log.warning(
            "Cold-start repo (0 merge commits): falling back to micro_probe "
            "for task_type=%s",
            task_type,
        )
        return "micro_probe"

    # --- Comprehension generator availability ---
    if task_type == "architecture_comprehension":
        if not _comprehension_generator_available():
            _log.warning(
                "Comprehension generator not available: falling back to "
                "micro_probe for task_type=architecture_comprehension"
            )
            return "micro_probe"

    return task_type


_CLI_DEFAULTS = {
    "count": 5,
    "source": "auto",
    "min_files": 0,
    "min_quality": 0.5,
    "enrich": False,
    "org_scale": False,
    "mcp_families": False,
}

# Deprecated legacy preset aliases. The --preset flag is kept as a backwards
# compatible alias that translates to a goal (with optional count override)
# and emits a deprecation warning. New code should use --goal directly.
#
# Shape: preset_name -> (goal_name, extra_overrides)
_PRESET_ALIASES: dict[str, tuple[str, dict]] = {
    "quick": ("general", {"count": 3}),
    "mcp": ("mcp", {}),
}

# ---------------------------------------------------------------------------
# User-defined mine profiles
# ---------------------------------------------------------------------------

# Keys that profiles may contain (matching Click parameter names).
_PROFILE_KEYS = frozenset(_CLI_DEFAULTS) | {
    "subsystem",
    "discover_subsystems",
    "no_llm",
    "family",
    "repos",
    "scan_timeout",
    "validate_flag",
    "curate",
    "backends",
    "verify_curation_flag",
    "sg_repo",
    "interactive",
    "preset",
    "goal",
}


def _user_profiles_path() -> Path:
    """Return ~/.codeprobe/mine-profiles.json."""
    return Path.home() / ".codeprobe" / "mine-profiles.json"


def _project_profiles_path(repo_path: Path | None = None) -> Path:
    """Return .codeprobe/mine-profiles.json relative to *repo_path* or cwd."""
    base = repo_path if repo_path is not None else Path.cwd()
    return base / ".codeprobe" / "mine-profiles.json"


def _load_profiles_from(path: Path) -> dict[str, dict]:
    """Load profiles from a JSON file, returning an empty dict on missing/invalid."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(v, dict)}
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def load_all_profiles(repo_path: Path | None = None) -> dict[str, tuple[dict, str]]:
    """Load profiles from user and project levels.

    Returns ``{name: (profile_dict, source_label)}`` where *source_label* is
    ``"user"`` or ``"project"``.  Project-level profiles override user-level
    profiles with the same name.
    """
    merged: dict[str, tuple[dict, str]] = {}
    for name, prof in _load_profiles_from(_user_profiles_path()).items():
        merged[name] = (prof, "user")
    for name, prof in _load_profiles_from(_project_profiles_path(repo_path)).items():
        merged[name] = (prof, "project")
    return merged


def load_profile(name: str, repo_path: Path | None = None) -> dict:
    """Load a single profile by *name*.

    Project-level profiles take precedence over user-level ones.
    Raises ``click.UsageError`` if the profile is not found.
    """
    all_profiles = load_all_profiles(repo_path)
    entry = all_profiles.get(name)
    if entry is None:
        available = ", ".join(sorted(all_profiles)) if all_profiles else "(none)"
        raise click.UsageError(
            f"Profile '{name}' not found. Available profiles: {available}"
        )
    return entry[0]


def save_profile(name: str, values: dict) -> Path:
    """Save *values* as profile *name* to the user-level config file.

    Creates ``~/.codeprobe/`` if it doesn't exist.  Returns the file path.
    """
    path = _user_profiles_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_profiles_from(path)
    existing[name] = {k: v for k, v in values.items() if k in _PROFILE_KEYS}
    path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    return path


def list_profiles(repo_path: Path | None = None) -> list[tuple[str, str, dict]]:
    """Return ``[(name, source_label, profile_dict), ...]`` sorted by name."""
    all_profiles = load_all_profiles(repo_path)
    return sorted((name, source, prof) for name, (prof, source) in all_profiles.items())


def resolve_effective_config(
    *,
    goal: str | None,
    preset: str | None,
    count: int,
    source: str,
    min_files: int,
    enrich: bool,
    org_scale: bool,
    mcp_families: bool,
    min_quality: float = 0.5,
    explicit_set: frozenset[str] = frozenset(),
    profile_set: frozenset[str] = frozenset(),
    warn: Callable[[str], None] | None = None,
) -> dict:
    """Resolve the effective config from goal, deprecated preset, and flags.

    Precedence (highest wins):
      1. Explicit CLI flags (``explicit_set``)
      2. Profile-loaded values (``profile_set``)
      3. Goal extras (applied only to keys in neither set AND still at
         their Click default)
      4. Click defaults

    Note on min_files=0: ``0`` is both the Click default and a valid user
    intent. CLI callers pass ``explicit_set`` to disambiguate; profile-loaded
    callers pass ``profile_set`` to mark intentional values. A programmatic
    caller that *means* ``min_files=0`` should include ``"min_files"`` in
    one of those sets.

    ``preset`` is a deprecated alias. If set, it is translated to a goal plus
    an optional override dict and a deprecation warning is emitted via
    *warn*. Passing both ``--preset`` and ``--goal`` with *different* values
    raises ``click.UsageError``.

    Returns a dict containing the final flag values plus the resolved goal
    name under ``"goal"``. ``warn`` is only called when the deprecated
    ``--preset`` flag is used.
    """
    effective: dict = {
        "count": count,
        "source": source,
        "min_files": min_files,
        "min_quality": min_quality,
        "enrich": enrich,
        "org_scale": org_scale,
        "mcp_families": mcp_families,
        "goal": goal,
    }
    protected = explicit_set | profile_set

    def _apply_overrides(overrides: dict) -> None:
        """Apply overrides to flags neither explicit nor profile-set, still
        at their Click default."""
        for key, value in overrides.items():
            if key in protected:
                continue
            if effective.get(key) == _CLI_DEFAULTS.get(key):
                effective[key] = value

    # Step 1: translate deprecated preset → goal.
    if preset is not None:
        if preset not in _PRESET_ALIASES:
            raise click.UsageError(
                f"Unknown preset '{preset}'. "
                f"Choose from: {', '.join(sorted(_PRESET_ALIASES))}"
            )
        preset_goal, preset_overrides = _PRESET_ALIASES[preset]

        if warn is not None:
            warn(
                "--preset is deprecated; use --goal "
                f"{preset_goal} instead (alias: --preset {preset})."
            )

        if goal is not None and goal != preset_goal:
            raise click.UsageError(
                f"Cannot use both --preset {preset} and --goal {goal}; "
                "--preset is deprecated, use --goal only."
            )

        if goal is None:
            effective["goal"] = preset_goal
            goal = preset_goal

        _apply_overrides(preset_overrides)

    # Step 2: apply goal extras.
    if goal is not None:
        if goal not in _EVAL_GOALS:
            raise click.UsageError(
                f"Unknown goal '{goal}'. "
                f"Choose from: {', '.join(sorted(_EVAL_GOALS))}"
            )
        _apply_overrides(_EVAL_GOALS[goal]["extras"])

    return effective


# ---------------------------------------------------------------------------
# Task-type dispatch
# ---------------------------------------------------------------------------


def _dispatch_by_task_type(
    *,
    task_type: str,
    repo_path: Path,
    count: int,
    source: str,
    min_files: int,
    subsystems: tuple[str, ...],
    no_llm: bool,
    enrich: bool,
    goal_name: str,
    bias: str,
    min_quality: float = 0.5,
    dual_verify: bool = False,
    narrative_source: tuple[str, ...] = (),
) -> None:
    """Route to the correct generation pipeline based on *task_type*.

    The registry in :mod:`codeprobe.mining.task_types` maps each task type
    to a ``dispatch_key`` (``"sdlc"`` / ``"probe"`` / ``"comprehension"`` /
    ``"mixed"``). Each key corresponds to an entry in
    ``_DISPATCH_HANDLERS`` below; adding a new task type that reuses an
    existing pipeline requires only a registry entry, not a new branch.

    ``org_scale_cross_repo`` is handled upstream via the ``--org-scale``
    flag path in :func:`run_mine` (it has a dedicated multi-repo scanning
    pipeline that doesn't share the single-repo dispatch surface here).
    """
    from codeprobe.mining.task_types import TASK_TYPE_REGISTRY

    common_kwargs = dict(
        repo_path=repo_path,
        count=count,
        source=source,
        min_files=min_files,
        min_quality=min_quality,
        subsystems=subsystems,
        no_llm=no_llm,
        enrich=enrich,
        goal_name=goal_name,
        bias=bias,
        dual_verify=dual_verify,
        narrative_source=narrative_source,
    )

    def _sdlc() -> None:
        _dispatch_sdlc(**common_kwargs)

    def _probe() -> None:
        _dispatch_probes(
            repo_path=repo_path,
            count=count,
            goal_name=goal_name,
            bias=bias,
        )

    def _comprehension() -> None:
        _dispatch_comprehension(
            repo_path=repo_path,
            count=count,
            goal_name=goal_name,
            bias=bias,
        )

    def _mixed() -> None:
        _dispatch_mixed(**common_kwargs)

    _DISPATCH_HANDLERS = {  # noqa: N806
        "sdlc": _sdlc,
        "probe": _probe,
        "comprehension": _comprehension,
        "mixed": _mixed,
    }

    info = TASK_TYPE_REGISTRY.get(task_type)
    if info is None or info.dispatch_key not in _DISPATCH_HANDLERS:
        _log.warning(
            "Unknown or unroutable task_type '%s'; falling back to SDLC mining",
            task_type,
        )
        _sdlc()
        return
    _DISPATCH_HANDLERS[info.dispatch_key]()


def _dispatch_cross_repo(
    *,
    primary: Path,
    cross_repo: tuple[str, ...],
    count: int,
    goal_name: str,
    bias: str,
    dual_verify: bool = False,
) -> None:
    """Dispatch cross-repo task mining.

    Resolves each ``--cross-repo`` entry to a local path (cloning if
    needed), selects a symbol resolver (Sourcegraph when auth is
    available, RipgrepResolver as fallback), and invokes
    :func:`mine_tasks_multi`.
    """
    from codeprobe.mining.multi_repo import (
        RipgrepResolver,
        mine_tasks_multi,
    )
    from codeprobe.mining.writer import write_task_dir

    # Resolve secondary repos
    secondaries: list[Path] = []
    for entry in cross_repo:
        if _is_git_url(entry):
            secondaries.append(_clone_repo(entry))
        else:
            rp = Path(entry).resolve()
            if not rp.exists():
                suggestion = _suggest_path(rp)
                hint = f" Did you mean: {suggestion}?" if suggestion else ""
                raise click.UsageError(
                    f"--cross-repo path does not exist: {rp}.{hint}"
                )
            secondaries.append(rp)

    # Select symbol resolver: prefer Sourcegraph, fall back to ripgrep
    from codeprobe.mining.sg_auth import AuthError, get_valid_token

    try:
        get_valid_token()
        from codeprobe.mining.sg_ground_truth import SourcegraphSymbolResolver

        resolver = SourcegraphSymbolResolver()
        click.echo("Using SourcegraphSymbolResolver for cross-repo mining")
    except AuthError:
        resolver = RipgrepResolver()
        click.echo(
            "Warning: No Sourcegraph auth found — falling back to ripgrep for "
            "cross-repo symbol resolution. Run `codeprobe auth sourcegraph` "
            "for higher-accuracy results.",
            err=True,
        )

    result = mine_tasks_multi(
        primary=primary,
        secondaries=tuple(secondaries),
        count=count,
        symbol_resolver=resolver,
    )

    if not result.tasks:
        click.echo(
            "No cross-repo tasks found. The primary repo may not have PRs "
            "that modify public symbols referenced in the secondary repos."
        )
        return

    tasks = list(result.tasks)
    if dual_verify:
        tasks = _apply_dual_verification(tasks, result, primary)

    tasks_dir = _clear_tasks_dir(primary)
    for task in tasks:
        write_task_dir(task, tasks_dir, primary)

    _record_task_ids_in_experiment(primary, [t.id for t in tasks])
    _show_results_table(tasks)
    _finish_mine_output(
        tasks,
        tasks_dir,
        goal_name,
        bias,
        (),
        primary,
        task_types=("sdlc_code_change",),
    )


def _apply_dual_verification(
    tasks: list[Task],
    mine_result: MineResult,
    repo_path: Path,
) -> list[Task]:
    """Apply dual verification to tasks: build oracle ground truth from PR diffs.

    For each task, generates oracle ground truth from changed files.
    Tasks with non-trivial oracles get ``verification_mode="dual"`` and
    oracle data populated. Tasks with empty oracles (all test files)
    fall back to ``verification_mode="test_script"`` only.

    Only applies to eligible task types (comprehension, org-scale, cross-repo).
    SDLC tasks are passed through unchanged per R16 constraint.
    """
    from codeprobe.mining.extractor import (
        _build_oracle_ground_truth,
        _oracle_discrimination_passed,
    )

    _DUAL_ELIGIBLE_CATEGORIES = frozenset(  # noqa: N806
        {
            "comprehension",
            "org_scale",
            "cross_repo",
        }
    )

    result: list[Task] = []
    dual_count = 0

    for task in tasks:
        # R16: only apply to task types with orthogonal artifact signal
        if task.metadata.category not in _DUAL_ELIGIBLE_CATEGORIES:
            result.append(task)
            continue

        if not task.metadata.ground_truth_commit:
            result.append(task)
            continue

        changed_files = mine_result.changed_files_map.get(task.id, [])
        if not changed_files:
            result.append(task)
            continue

        oracle = _build_oracle_ground_truth(
            merge_sha=task.metadata.ground_truth_commit,
            repo_path=repo_path,
            changed_files=changed_files,
        )

        if oracle is None:
            # All test files — fall back to test_script only
            result.append(task)
            continue

        passed, confidence = _oracle_discrimination_passed(oracle)
        if not passed:
            result.append(task)
            continue

        # Build dual task with oracle data
        new_verification = replace(
            task.verification,
            verification_mode="dual",
            oracle_type=oracle["answer_type"],
            oracle_answer=tuple(oracle["answer"]),
        )
        new_task = replace(
            task,
            verification=new_verification,
        )
        result.append(new_task)
        dual_count += 1

        if confidence == "low":
            click.echo(
                f"  ! {task.id}: low-confidence oracle "
                f"({len(oracle['answer'])} files, mostly one directory)"
            )

    if dual_count:
        click.echo(f"Applied dual verification to {dual_count}/{len(tasks)} tasks")

    return result


def _mine_tasks_with_progress(
    repo_path: Path,
    *,
    count: int,
    source_hint: str,
    min_files: int,
    min_quality: float,
    subsystems: tuple[str, ...],
) -> "MineResult":
    """Call :func:`mine_tasks` with a click.progressbar when stderr is a TTY.

    Falls back to a plain call (no bar) in non-interactive environments so
    CI logs stay clean. The bar length is derived from ``mine_tasks``' own
    internal search-limit (``count*4`` or ``count*8``), so it only tracks
    the per-PR scoring loop — the subsequent LLM enrichment and writing
    phases print their own progress messages.
    """
    from codeprobe.mining import mine_tasks

    search_limit = count * 4 if min_files == 0 else count * 8
    use_bar = sys.stderr.isatty()

    if not use_bar:
        click.echo(
            f"Analyzing up to {search_limit} merge commits...", err=True
        )
        return mine_tasks(
            repo_path,
            count=count,
            source_hint=source_hint,
            min_files=min_files,
            min_quality=min_quality,
            subsystems=subsystems,
        )

    with click.progressbar(
        length=search_limit,
        label="Analyzing merge commits",
        file=sys.stderr,
    ) as bar:
        return mine_tasks(
            repo_path,
            count=count,
            source_hint=source_hint,
            min_files=min_files,
            min_quality=min_quality,
            subsystems=subsystems,
            progress=bar.update,
        )


def _resolve_narrative_source(
    narrative_source: tuple[str, ...],
    repo_path: Path,
    *,
    tasks_mined: bool = True,
    pr_bodies: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """Validate ``--narrative-source`` and enforce INV1 (no silent fallback).

    Returns the resolved adapter-name selection. Behavior:

    * If the user passed ``--narrative-source`` explicitly, validate each
      name and return the parsed tuple.
    * If omitted and ``tasks_mined`` is True:
        * When ``pr_bodies`` contains at least one non-empty body, OR the
          ``gh`` CLI reports at least one merged PR, default to
          ``("pr",)`` (backward compat).
        * Otherwise raise ``click.UsageError`` naming
          ``--narrative-source commits+rfcs`` as the required fix.
    * If omitted and ``tasks_mined`` is False, return ``()`` — mining
      already yielded nothing so the user will see the "No suitable
      tasks" message rather than the INV1 prompt.

    ``pr_bodies`` is an optional shortcut: when the caller has already
    run the miner, a non-empty body is direct evidence of PR narratives
    and avoids a second ``gh`` round-trip (also avoids test-side subprocess
    patching gymnastics).
    """
    from codeprobe.mining.sources import (
        has_pr_narratives,
        parse_narrative_selection,
        select_narrative_adapters,
    )

    if narrative_source:
        selection = parse_narrative_selection(narrative_source)
        if not selection:
            raise click.UsageError(
                "--narrative-source was passed but parsed to an empty "
                "selection. Accepted names: pr, commits, rfcs."
            )
        try:
            select_narrative_adapters(selection)  # validate names
        except ValueError as exc:
            raise click.UsageError(str(exc)) from exc
        return selection

    if not tasks_mined:
        # Nothing to enrich; short-circuit so zero-task test fixtures
        # don't need to mock has_pr_narratives.
        return ()

    # Fast path: if the miner already produced a non-empty PR body for any
    # candidate, we have narrative context. Default to the ``pr`` adapter.
    if pr_bodies and any(body.strip() for body in pr_bodies.values()):
        return ("pr",)

    # Slower fallback: probe gh to see if the repo has PRs at all. Needed
    # when mining yielded tasks but every PR body happened to be empty.
    if has_pr_narratives(repo_path):
        return ("pr",)

    raise click.UsageError(
        "No merged PR narratives found in this repo (squash-only or "
        "no-remote history), and --narrative-source was not passed. "
        "INV1 requires explicit selection: pass e.g. "
        "--narrative-source commits+rfcs (or --narrative-source commits, "
        "or --narrative-source rfcs). Accepted names: pr, commits, rfcs."
    )


def _dispatch_sdlc(
    *,
    repo_path: Path,
    count: int,
    source: str,
    min_files: int,
    subsystems: tuple[str, ...],
    no_llm: bool,
    enrich: bool,
    goal_name: str,
    bias: str,
    min_quality: float = 0.5,
    dual_verify: bool = False,
    narrative_source: tuple[str, ...] = (),
) -> None:
    """Run PR-based SDLC mining pipeline."""
    from codeprobe.mining import write_task_dir

    mine_result = _mine_tasks_with_progress(
        repo_path,
        count=count,
        source_hint=source,
        min_files=min_files,
        min_quality=min_quality,
        subsystems=subsystems,
    )
    tasks = mine_result.tasks

    if tasks:
        # INV1 loud-error guard: if mining produced tasks but no
        # --narrative-source was passed AND the repo has no PR narratives,
        # that means the pipeline fell through to commit-message bare tier
        # silently. Raise here so users know to pass --narrative-source
        # explicitly. Zero-task test fixtures skip this block. We feed the
        # already-mined pr_bodies in so evidence of PR narratives short-
        # circuits the gh probe.
        resolved_selection = _resolve_narrative_source(
            narrative_source,
            repo_path,
            tasks_mined=True,
            pr_bodies=mine_result.pr_bodies,
        )

        # Stamp the adapter trail onto every mined task so downstream
        # writers can surface which narrative source fed the enrichment.
        # LLM enrichment (if it runs) appends "+llm". We no-op on non-
        # dataclass tasks (MagicMock from tests) rather than crash.
        trail = "+".join(resolved_selection) if resolved_selection else "pr"
        stamped: list[Task] = []
        for task in tasks:
            if is_dataclass(task) and is_dataclass(task.metadata):
                stamped.append(
                    replace(
                        task,
                        metadata=replace(
                            task.metadata, enrichment_source=trail
                        ),
                    )
                )
            else:  # pragma: no cover — defensive for MagicMock-based tests
                stamped.append(task)
        tasks = stamped
        if is_dataclass(mine_result):
            mine_result = replace(mine_result, tasks=tasks)

    if not tasks:
        click.echo(
            "No suitable tasks found. Try a repo with merged PRs that include tests."
        )
        return

    if (
        mine_result.min_files_used is not None
        and mine_result.min_files_used < min_files
    ):
        click.echo(
            f"  ⚠ Relaxed min_files from {min_files} to "
            f"{mine_result.min_files_used} (no candidates at original threshold)"
        )

    tasks = _enrich_sdlc_tasks(tasks, mine_result, no_llm, enrich)

    # Apply dual verification when --dual-verify is set
    if dual_verify:
        tasks = _apply_dual_verification(tasks, mine_result, repo_path)

    llm_used = _was_llm_used(no_llm)

    tasks_dir = _clear_tasks_dir(repo_path)
    for task in tasks:
        write_task_dir(
            task,
            tasks_dir,
            repo_path,
            ground_truth=mine_result.ground_truth_map.get(task.id),
        )

    _record_task_ids_in_experiment(repo_path, [t.id for t in tasks])
    _show_results_table(tasks)
    _finish_mine_output(
        tasks,
        tasks_dir,
        goal_name,
        bias,
        subsystems,
        repo_path,
        task_types=("sdlc_code_change",),
        llm_enriched=llm_used,
    )


def _dispatch_probes(
    *,
    repo_path: Path,
    count: int,
    goal_name: str,
    bias: str,
) -> None:
    """Generate micro-benchmark probe tasks."""
    from codeprobe.probe.adapter import ProbeTaskAdapter
    from codeprobe.probe.generator import generate_probes

    probes = generate_probes(repo_path, count=count)

    if not probes:
        click.echo(
            "No probes generated. The repo may not contain enough "
            "extractable symbols (functions, classes)."
        )
        return

    tasks_dir = _clear_tasks_dir(repo_path)
    created = ProbeTaskAdapter.convert_batch(
        probes, tasks_dir, repo_name=repo_path.name
    )

    _record_task_ids_in_experiment(repo_path, [p.name for p in created])

    click.echo()
    click.echo(f"Generated {len(created)} probe tasks:")
    click.echo()
    for i, p in enumerate(created, 1):
        click.echo(f"  {i:>2}  {p.name}")
    # Emit suite.toml for probe tasks
    from codeprobe.mining.writer import write_suite_manifest

    suite_path = write_suite_manifest(
        tasks_dir=tasks_dir,
        goal_name=goal_name,
        task_types=("micro_probe",),
        description=f"Generated by codeprobe mine --goal ({goal_name})",
    )
    _print_summary_block(
        task_count=len(created),
        quality_warning_count=0,
        tasks_dir=tasks_dir,
        suite_path=suite_path,
    )
    _show_next_steps(repo_path, 0, tasks_dir=tasks_dir)


def _dispatch_comprehension(
    *,
    repo_path: Path,
    count: int,
    goal_name: str,
    bias: str,
) -> None:
    """Generate architecture comprehension tasks."""
    from codeprobe.mining.comprehension import ComprehensionGenerator
    from codeprobe.mining.writer import write_task_dir

    generator = ComprehensionGenerator(repo_path)
    tasks = generator.generate(count=count)

    if not tasks:
        click.echo(
            "No comprehension tasks generated. The repo may not have enough "
            "import structure for transitive reasoning tasks."
        )
        return

    tasks_dir = _clear_tasks_dir(repo_path)
    for task in tasks:
        write_task_dir(task, tasks_dir, repo_path)

    _record_task_ids_in_experiment(repo_path, [t.id for t in tasks])
    _show_results_table(tasks)
    _finish_mine_output(
        tasks,
        tasks_dir,
        goal_name,
        bias,
        (),
        repo_path,
        task_types=("architecture_comprehension",),
    )


def _dispatch_mixed(
    *,
    repo_path: Path,
    count: int,
    source: str,
    min_files: int,
    subsystems: tuple[str, ...],
    no_llm: bool,
    enrich: bool,
    goal_name: str,
    bias: str,
    min_quality: float = 0.5,
    dual_verify: bool = False,
    narrative_source: tuple[str, ...] = (),
) -> None:
    """Run SDLC mining + probe generation, combining results."""
    from codeprobe.mining import write_task_dir as write_mining_task
    from codeprobe.probe.adapter import ProbeTaskAdapter
    from codeprobe.probe.generator import generate_probes

    # Split count: half SDLC, half probes (at least 1 each when count >= 2)
    sdlc_count = max(1, count // 2)
    probe_count = max(1, count - sdlc_count)

    tasks_dir = _clear_tasks_dir(repo_path)
    all_task_ids: list[str] = []
    sdlc_tasks: list = []

    # SDLC mining (may produce 0 tasks on cold-start repos)
    mine_result = _mine_tasks_with_progress(
        repo_path,
        count=sdlc_count,
        source_hint=source,
        min_files=min_files,
        min_quality=min_quality,
        subsystems=subsystems,
    )
    sdlc_tasks = mine_result.tasks
    if sdlc_tasks:
        resolved_selection = _resolve_narrative_source(
            narrative_source,
            repo_path,
            tasks_mined=True,
            pr_bodies=mine_result.pr_bodies,
        )
        trail = "+".join(resolved_selection) if resolved_selection else "pr"
        stamped: list[Task] = []
        for t in sdlc_tasks:
            if is_dataclass(t) and is_dataclass(t.metadata):
                stamped.append(
                    replace(t, metadata=replace(t.metadata, enrichment_source=trail))
                )
            else:  # pragma: no cover — MagicMock-based tests
                stamped.append(t)
        sdlc_tasks = stamped
        sdlc_tasks = _enrich_sdlc_tasks(sdlc_tasks, mine_result, no_llm, enrich)
        if dual_verify:
            sdlc_tasks = _apply_dual_verification(sdlc_tasks, mine_result, repo_path)
        for task in sdlc_tasks:
            write_mining_task(
                task,
                tasks_dir,
                repo_path,
                ground_truth=mine_result.ground_truth_map.get(task.id),
            )
        all_task_ids.extend(t.id for t in sdlc_tasks)

    # Probe generation
    probes = generate_probes(repo_path, count=probe_count)
    probe_dirs: list[Path] = []
    if probes:
        probe_dirs = ProbeTaskAdapter.convert_batch(
            probes, tasks_dir, repo_name=repo_path.name
        )
        all_task_ids.extend(p.name for p in probe_dirs)

    if not sdlc_tasks and not probes:
        click.echo(
            "No tasks generated. Try a repo with merged PRs or more "
            "extractable symbols."
        )
        return

    _record_task_ids_in_experiment(repo_path, all_task_ids)

    # Show combined results
    if sdlc_tasks:
        _show_results_table(sdlc_tasks)
    if probe_dirs:
        click.echo(f"Generated {len(probe_dirs)} probe tasks:")
        for i, p in enumerate(probe_dirs, 1):
            click.echo(f"  {i:>2}  {p.name}")
        click.echo()

    # Emit suite.toml — union of whatever was actually generated
    from codeprobe.mining.writer import write_suite_manifest

    mixed_types: list[str] = []
    if sdlc_tasks:
        mixed_types.append("sdlc_code_change")
    if probe_dirs:
        mixed_types.append("micro_probe")
    suite_path: Path | None = None
    if mixed_types:
        suite_path = write_suite_manifest(
            tasks_dir=tasks_dir,
            goal_name=goal_name,
            task_types=tuple(mixed_types),
            description=f"Generated by codeprobe mine --goal ({goal_name})",
        )

    llm_used = _was_llm_used(no_llm)
    total_count = len(sdlc_tasks) + len(probe_dirs)
    _print_summary_block(
        task_count=total_count,
        quality_warning_count=0,
        tasks_dir=tasks_dir,
        suite_path=suite_path,
        llm_enriched=llm_used,
    )
    if subsystems:
        click.echo(f"Subsystems: {', '.join(subsystems)}")
        click.echo()
    _show_next_steps(
        repo_path, min_files, llm_enriched=llm_used, tasks_dir=tasks_dir
    )


def _finish_mine_output(
    tasks: list,
    tasks_dir: Path,
    goal_name: str,
    bias: str,
    subsystems: tuple[str, ...],
    repo_path: Path,
    task_types: tuple[str, ...] = (),
    *,
    llm_enriched: bool = False,
) -> None:
    """Shared output: quality warnings, summary block, and next steps."""
    from codeprobe.mining.writer import write_suite_manifest

    warnings = _quality_review(tasks, goal_name, bias)
    if warnings:
        click.echo("Quality warnings:")
        for w in warnings:
            click.echo(f"  ! {w}")
        click.echo()

    # Emit suite.toml alongside the tasks directory
    suite_path: Path | None = None
    if task_types:
        suite_path = write_suite_manifest(
            tasks_dir=tasks_dir,
            goal_name=goal_name,
            task_types=task_types,
            description=f"Generated by codeprobe mine --goal ({goal_name})",
        )

    _print_summary_block(
        task_count=len(tasks),
        quality_warning_count=len(warnings),
        tasks_dir=tasks_dir,
        suite_path=suite_path,
        llm_enriched=llm_enriched,
    )
    if subsystems:
        click.echo(f"Subsystems: {', '.join(subsystems)}")
        click.echo()
    _show_next_steps(
        repo_path, 0, llm_enriched=llm_enriched, tasks_dir=tasks_dir
    )


def run_mine(
    path: str,
    preset: str | None = None,
    goal: str | None = None,
    task_type_override: str | None = None,
    count: int = 5,
    cross_repo: tuple[str, ...] = (),
    source: str = "auto",
    min_files: int = 0,
    min_quality: float = 0.5,
    subsystems: tuple[str, ...] = (),
    discover_subsystems: bool = False,
    enrich: bool = False,
    interactive: bool | None = None,
    no_llm: bool = False,
    org_scale: bool = False,
    families: tuple[str, ...] = (),
    repos: tuple[str, ...] = (),
    scan_timeout: int = 60,
    validate_flag: bool = False,
    curate: bool = False,
    backends: tuple[str, ...] = (),
    verify_curation_flag: bool = False,
    mcp_families: bool = False,
    sg_repo: str = "",
    sg_discovery: bool = False,
    dual_verify: bool = False,
    narrative_source: tuple[str, ...] = (),
    refresh_dir: str | None = None,
    accept_structural_change: bool = False,
    explicit_set: frozenset[str] = frozenset(),
    profile_set: frozenset[str] = frozenset(),
    json_flag: bool = False,
    no_json_flag: bool = False,
    json_lines_flag: bool = False,
) -> None:
    """Mine eval tasks from a repository."""
    from codeprobe.cli._output_helpers import emit_envelope, resolve_mode

    global _MINE_START_TIME
    _MINE_START_TIME = time.monotonic()

    _mine_mode = resolve_mode(
        "mine", json_flag, no_json_flag, json_lines_flag,
    )

    # Refresh dispatch: runs before any other mining path so users don't
    # accidentally re-mine a whole tasks dir when they only meant to
    # refresh a single task.
    if refresh_dir is not None:
        _run_refresh(
            Path(refresh_dir).resolve(),
            repo_path_arg=path,
            accept_structural_change=accept_structural_change,
        )
        return

    # CLI validation: --cross-repo and --org-scale are mutually exclusive
    if cross_repo and org_scale:
        raise click.UsageError(
            "Cannot use --cross-repo with --org-scale. "
            "Use --cross-repo for cross-repo SDLC mining or "
            "--org-scale for org-scale comprehension mining."
        )

    # Default goal to mcp when --cross-repo is used without explicit --goal
    if cross_repo and goal is None and "goal" not in explicit_set:
        click.echo("Defaulting to --goal mcp for cross-repo mining")
        goal = "mcp"

    # Resolve goal, deprecated preset alias, and flag extras in one pass.
    # Any extras from a goal (e.g. --goal mcp → org_scale=True, min_files=6)
    # take effect *before* the org-scale dispatch branch below.
    resolved = resolve_effective_config(
        goal=goal,
        preset=preset,
        count=count,
        source=source,
        min_files=min_files,
        min_quality=min_quality,
        enrich=enrich,
        org_scale=org_scale,
        mcp_families=mcp_families,
        explicit_set=explicit_set,
        profile_set=profile_set,
        warn=lambda msg: click.echo(f"Warning: {msg}", err=True),
    )
    count = resolved["count"]
    source = resolved["source"]
    min_files = resolved["min_files"]
    min_quality = resolved["min_quality"]
    enrich = resolved["enrich"]
    org_scale = resolved["org_scale"]
    mcp_families = resolved["mcp_families"]
    goal = resolved["goal"]

    # Derive display name, bias, and task_type from the resolved goal, if any.
    # min_files is already resolved via goal extras inside
    # resolve_effective_config, so we only pull the descriptive fields here.
    if goal is not None:
        goal_entry = _EVAL_GOALS[goal]
        goal_name = goal_entry["name"]
        bias = goal_entry["bias"]
        task_type = goal_entry["task_type"]
    else:
        goal_name = "General benchmarking"
        bias = "balanced"
        task_type = "mixed"

    # --task-type takes precedence over the goal-derived task_type.
    if task_type_override is not None:
        task_type = task_type_override
        goal_name = f"{goal_name} (task-type={task_type})"
        # org_scale_cross_repo routes through the org-scale pipeline,
        # which is gated on the separate --org-scale flag path. Auto-
        # enable it so --task-type alone is enough.
        if task_type == "org_scale_cross_repo":
            org_scale = True

    # CLI validation: --backends agent --no-llm is incompatible
    if no_llm and "agent" in backends:
        raise click.UsageError(
            "Cannot use --backends agent with --no-llm: "
            "AgentSearchBackend requires an LLM backend."
        )

    # AC1: when the default path '.' is used and cwd isn't a git repo, prompt
    # for a usable path rather than bailing out with a hard error. This is the
    # "guided flow for missing inputs" case — the user ran `codeprobe mine`
    # from a non-repo directory, which is easy to do on first use.
    if path == "." and _is_interactive():
        cwd_is_repo = (Path.cwd() / ".git").exists()
        if not cwd_is_repo:
            click.echo(
                f"Current directory ({Path.cwd()}) is not a git repository."
            )
            path = click.prompt(
                "Path to a local git repo (or a git URL to clone)",
                type=str,
            )

    repo_path = _resolve_repo_path(path)

    # Non-blocking suitability check applies to every dispatch path
    # (cross-repo, org-scale, and the single-repo pipelines below).
    # Runs once here, before any mining happens.
    if interactive is None:
        interactive = _is_interactive()
    if not _run_suitability_check(
        task_type,
        repo_path,
        interactive=bool(interactive),
    ):
        click.echo("Aborted.")
        return

    try:
        # Cross-repo dispatch: AFTER resolve_effective_config, BEFORE org_scale
        if cross_repo:
            _dispatch_cross_repo(
                primary=repo_path,
                cross_repo=cross_repo,
                count=count,
                goal_name=goal_name,
                bias=bias,
                dual_verify=dual_verify,
            )
            return

        if org_scale:
            # Build repo_paths list: primary path + any --repos entries
            repo_paths = [repo_path]
            for r in repos:
                if _is_git_url(r):
                    repo_paths.append(_clone_repo(r))
                else:
                    rp = Path(r).resolve()
                    if not rp.exists():
                        suggestion = _suggest_path(rp)
                        hint = (
                            f" Did you mean: {suggestion}?" if suggestion else ""
                        )
                        raise click.UsageError(
                            f"--repos path does not exist: {rp}.{hint}"
                        )
                    repo_paths.append(rp)
            _run_org_scale_mine(
                repo_paths,
                count=count,
                no_llm=no_llm,
                families=families,
                scan_timeout=scan_timeout,
                validate_flag=validate_flag,
                curate=curate,
                backends=backends,
                verify_curation_flag=verify_curation_flag,
                mcp_families=mcp_families,
                sg_repo=sg_repo,
                sg_discovery=sg_discovery,
                dual_verify=dual_verify,
            )
            return

        if interactive and goal is None:
            (
                goal_name,
                count,
                source,
                min_files,
                bias,
                task_type,
                subsystems,
                discover_subsystems,
            ) = _interactive_config(
                count, source, min_files, subsystems, discover_subsystems, repo_path
            )

        # Apply cold-start and comprehension-availability fallbacks
        task_type = _resolve_task_type(task_type, repo_path, source)

        # Note: narrative-source selection (INV1) is resolved inside
        # _dispatch_sdlc / _dispatch_mixed — it needs to run only on
        # dispatch paths that actually consume narrative context, and
        # AFTER mining so zero-task fixtures (CI) do not need to mock
        # has_pr_narratives. See _resolve_narrative_source for the
        # loud-error contract.

        if discover_subsystems:
            subsystems = _discover_and_select(repo_path, source)
            if not subsystems:
                return

        subsystems = tuple(s if s.endswith("/") else s + "/" for s in subsystems)

        if interactive and not _show_preflight(
            repo_path, goal_name, count, source, min_files, bias, subsystems
        ):
            click.echo("Aborted.")
            return

        if interactive:
            click.echo("\nMining tasks...")

        # Dispatch based on resolved task_type
        _dispatch_by_task_type(
            task_type=task_type,
            repo_path=repo_path,
            count=count,
            source=source,
            min_files=min_files,
            min_quality=min_quality,
            subsystems=subsystems,
            no_llm=no_llm,
            enrich=enrich,
            goal_name=goal_name,
            bias=bias,
            dual_verify=dual_verify,
            narrative_source=narrative_source,
        )
    except KeyboardInterrupt:
        # AC3: clean up partial output and exit with the standard SIGINT code.
        partial = _CURRENT_TASKS_DIR
        if partial is not None and partial.exists():
            shutil.rmtree(partial, ignore_errors=True)
            click.echo(
                f"\nInterrupted. Removed partial output at {partial}.", err=True
            )
        else:
            click.echo("\nInterrupted.", err=True)
        sys.exit(130)

    # Success path: when envelope/NDJSON mode is active, emit a terminal
    # summary envelope. Pretty mode preserves the existing click.echo
    # output block untouched.
    if _mine_mode.mode in ("single_envelope", "ndjson"):
        tasks_dir = _CURRENT_TASKS_DIR
        task_count = 0
        tasks_dir_str: str | None = None
        if tasks_dir is not None:
            tasks_dir_str = str(tasks_dir)
            if tasks_dir.is_dir():
                task_count = sum(
                    1
                    for c in tasks_dir.iterdir()
                    if c.is_dir() and (c / "instruction.md").is_file()
                )
        emit_envelope(
            command="mine",
            data={
                "tasks_dir": tasks_dir_str,
                "task_count": task_count,
                "goal": goal,
            },
        )


# ---------------------------------------------------------------------------
# Org-scale mining pipeline
# ---------------------------------------------------------------------------


def _run_org_scale_mine(
    repo_paths: list[Path],
    *,
    count: int = 5,
    no_llm: bool = False,
    families: tuple[str, ...] = (),
    scan_timeout: int = 60,
    validate_flag: bool = False,
    curate: bool = False,
    backends: tuple[str, ...] = (),
    verify_curation_flag: bool = False,
    mcp_families: bool = False,
    sg_repo: str = "",
    sg_discovery: bool = False,
    dual_verify: bool = False,
) -> None:
    """Mine org-scale comprehension tasks with oracle verification.

    Note: ``dual_verify`` is accepted but not yet wired — org-scale tasks
    build oracles via a different pipeline (family-based pattern matching)
    that doesn't use the SDLC-style ``changed_files_map``. Phase 3 will
    add native dual oracle support for org-scale families.
    """
    from codeprobe.mining.org_scale import mine_org_scale_tasks
    from codeprobe.mining.org_scale_families import FAMILY_BY_NAME
    from codeprobe.mining.writer import write_task_dir

    primary_repo = repo_paths[0]
    repo_names = ", ".join(rp.name for rp in repo_paths)
    click.echo(f"Scanning {repo_names} for org-scale patterns...", err=True)

    # Filter families if specified
    selected_families: tuple[TaskFamily, ...] | None = None
    if families:
        selected = [FAMILY_BY_NAME[f] for f in families if f in FAMILY_BY_NAME]
        unknown = [f for f in families if f not in FAMILY_BY_NAME]
        if unknown:
            click.echo(f"Unknown families: {', '.join(unknown)}")
            click.echo(f"Available: {', '.join(FAMILY_BY_NAME)}")
            return
        selected_families = tuple(selected)

    # Interactive family selection when TTY and no explicit --family filter
    if not selected_families and _is_interactive():
        selected_families = _interactive_family_selection(repo_paths)
        if selected_families is not None and not selected_families:
            click.echo("No families selected. Aborted.")
            return

    # Default sg_repo from primary repo name if not explicitly provided
    effective_sg_repo = sg_repo
    if not effective_sg_repo and mcp_families:
        effective_sg_repo = f"github.com/sg-evals/{repo_paths[0].name}"

    result = mine_org_scale_tasks(
        repo_paths,
        count=count,
        families=selected_families,
        no_llm=no_llm,
        scan_timeout=scan_timeout,
        include_mcp_families=mcp_families,
        sg_repo=effective_sg_repo,
        sg_discovery=sg_discovery,
    )

    if not result.tasks:
        click.echo(
            "No org-scale tasks generated. Repo may not have enough pattern matches."
        )
        if result.scan_results:
            click.echo("\nScan results (below min_hits threshold):")
            for sr in result.scan_results:
                click.echo(f"  {sr.family.name}: {len(sr.matched_files)} files")
        return

    # Show scan summary
    click.echo()
    click.echo("Scan results:")
    for sr in result.scan_results:
        click.echo(f"  {sr.family.name}: {len(sr.matched_files)} files matched")
    click.echo()

    # Run MCP delta validation if requested
    if validate_flag:
        _run_validation(result, repo_paths)

    # Run curation pipeline if requested
    curation_backends_used: tuple[str, ...] = ()
    curated_tasks = result.tasks
    if curate:
        curated_tasks, curation_backends_used = _run_curation(
            result,
            repo_paths,
            backends=backends,
            no_llm=no_llm,
            verify_curation_flag=verify_curation_flag,
        )

    # Write tasks
    tasks_dir = _clear_tasks_dir(primary_repo)
    for task in curated_tasks:
        write_task_dir(
            task,
            tasks_dir,
            primary_repo,
            curation_backends=curation_backends_used,
        )

    _record_task_ids_in_experiment(primary_repo, [t.id for t in curated_tasks])

    _show_org_scale_results(
        curated_tasks, tasks_dir, primary_repo, curation_backends_used
    )


def _build_curation_backends(
    backends: tuple[str, ...],
    no_llm: bool,
) -> list[object]:
    """Build list of CurationBackend instances from backend names.

    When *backends* is empty, uses defaults (grep + pr_diff; agent_search
    only when LLM is available).
    """
    from codeprobe.mining.curator_backends import (
        AgentSearchBackend,
        GrepBackend,
        PRDiffBackend,
        SourcegraphBackend,
    )

    _BACKEND_MAP = {  # noqa: N806
        "grep": GrepBackend,
        "sourcegraph": SourcegraphBackend,
        "pr_diff": PRDiffBackend,
        "agent": AgentSearchBackend,
    }

    if backends:
        return [_BACKEND_MAP[name]() for name in backends if name in _BACKEND_MAP]

    # Defaults: grep + pr_diff; agent_search only if LLM available
    result: list[object] = [GrepBackend(), PRDiffBackend()]
    if not no_llm:
        result.append(AgentSearchBackend())
    return result


def _run_curation(
    result: OrgScaleMineResult,
    repo_paths: list[Path],
    *,
    backends: tuple[str, ...] = (),
    no_llm: bool = False,
    verify_curation_flag: bool = False,
) -> tuple[list[Task], tuple[str, ...]]:
    """Run curation pipeline on mined tasks, returning updated tasks and backends used."""
    from codeprobe.mining.curator import CurationPipeline
    from codeprobe.mining.curator_tiers import classify_tiers, verify_curation
    from codeprobe.mining.org_scale import generate_org_scale_task

    backend_instances = _build_curation_backends(backends, no_llm)
    pipeline = CurationPipeline(backends=backend_instances)

    curated_tasks: list[Task] = []
    all_backends_used: set[str] = set()

    for sr in result.scan_results:
        # Run curation for this family
        curation_result = pipeline.curate(repos=repo_paths, family=sr.family)
        if not curation_result.files:
            # No curated files: keep original tasks for this family
            for task in result.tasks:
                if task.metadata.category == sr.family.name:
                    curated_tasks.append(task)
            continue

        all_backends_used.update(curation_result.backends_used)

        # Classify tiers
        tiered_files = classify_tiers(
            list(curation_result.files),
            sr.family,
            repo_paths,
            use_llm=not no_llm,
        )

        # Build updated CurationResult with tiered files
        from codeprobe.mining.curator import CurationResult

        tiered_result = CurationResult(
            family=curation_result.family,
            files=tuple(tiered_files),
            repo_paths=curation_result.repo_paths,
            commit_shas=curation_result.commit_shas,
            backends_used=curation_result.backends_used,
            merge_config=curation_result.merge_config,
            matched_files=frozenset(cf.path for cf in tiered_files),
        )

        # Verify curation if requested
        if verify_curation_flag and not no_llm:
            verdict = verify_curation(tiered_files, sr.family, repo_paths)
            click.echo(f"  Curation verification ({sr.family.name}): {verdict}")

        # Re-generate task with curation result
        for task in result.tasks:
            if task.metadata.category == sr.family.name:
                curated_task = generate_org_scale_task(
                    sr,
                    no_llm=no_llm,
                    curation_result=tiered_result,
                )
                if curated_task is not None:
                    curated_tasks.append(curated_task)
                    break  # One task per family/scan_result

    backends_tuple = tuple(sorted(all_backends_used))
    return curated_tasks, backends_tuple


def _interactive_family_selection(
    repo_paths: list[Path],
) -> tuple[TaskFamily, ...] | None:
    """Show detected families with hit counts and prompt for selection.

    Returns None to use all families (default), or a tuple of selected families.
    """
    from codeprobe.mining.org_scale_families import FAMILIES
    from codeprobe.mining.org_scale_scanner import get_tracked_files, scan_repo

    # Quick scan to show hit counts
    all_tracked: frozenset[str] = frozenset()
    for rp in repo_paths:
        all_tracked = all_tracked | get_tracked_files(rp)

    scan_results = scan_repo(repo_paths, FAMILIES, tracked_files=all_tracked)

    click.echo()
    click.echo("Detected task families:")
    entries = []
    for i, family in enumerate(FAMILIES, 1):
        sr = next((s for s in scan_results if s.family.name == family.name), None)
        hit_count = len(sr.matched_files) if sr else 0
        status = (
            f"{hit_count} files"
            if hit_count >= family.min_hits
            else f"{hit_count} files (below threshold)"
        )
        entries.append((family, hit_count))
        click.echo(f"  [{i}] {family.name:<30s} {status}")
    click.echo()

    raw = click.prompt(
        "Select families (comma-separated numbers, or Enter for all)",
        default="",
        show_default=False,
    )

    if not raw.strip():
        return None  # Use all families

    selected = []
    for token in raw.split(","):
        token = token.strip()
        try:
            idx = int(token)
            if 1 <= idx <= len(FAMILIES):
                selected.append(FAMILIES[idx - 1])
            else:
                click.echo(f"  Skipping out-of-range index: {idx}")
        except ValueError:
            click.echo(f"  Skipping invalid input: {token}")

    return tuple(selected) if selected else tuple()


def _run_validation(
    result: OrgScaleMineResult,
    repo_paths: list[Path],
) -> None:
    """Run MCP delta validation and display results."""
    from codeprobe.mining.org_scale_validate import validate_families

    # Group tasks by family for validation
    family_tasks: dict[str, list] = {}
    for task in result.tasks:
        family_tasks.setdefault(task.metadata.category, []).append(task)

    families_to_validate = []
    tasks_per_family = []
    repos_per_family = []
    for sr in result.scan_results:
        tasks_for_family = family_tasks.get(sr.family.name, [])
        if tasks_for_family:
            families_to_validate.append(sr.family)
            tasks_per_family.append(tasks_for_family)
            repos_per_family.append(repo_paths)

    if not families_to_validate:
        click.echo("No families to validate.")
        return

    click.echo("Running MCP delta validation...")
    delta_results = validate_families(
        families_to_validate, tasks_per_family, repos_per_family
    )
    click.echo()
    click.echo("Validation results:")
    for dr in delta_results:
        status = "BASELINE-ONLY" if dr.is_baseline_only else "OK"
        click.echo(f"  {dr.family_name:<30s} grep_f1={dr.grep_f1:.3f} [{status}]")
    click.echo()


def _show_org_scale_results(
    tasks: list[Task],
    tasks_dir: Path,
    repo_path: Path,
    curation_backends: tuple[str, ...] = (),
) -> None:
    """Display org-scale mining results table and next steps."""
    curated = bool(curation_backends)

    click.echo(f"Generated {len(tasks)} org-scale tasks:")
    click.echo()

    # Header — add Tiers column when curation is active
    if curated:
        click.echo(
            f"  {'#':>2}  {'Task ID':<14} {'Family':<24} {'Difficulty':<10} "
            f"{'Files':>5}  {'Tiers (R/S/C)':>13}"
        )
        click.echo("  " + "-" * 76)
    else:
        click.echo(
            f"  {'#':>2}  {'Task ID':<14} {'Family':<24} {'Difficulty':<10} "
            f"{'Files':>5}"
        )
        click.echo("  " + "-" * 60)

    for i, t in enumerate(tasks, 1):
        base = (
            f"  {i:>2}  {t.id:<14} {t.metadata.category:<24} "
            f"{t.metadata.difficulty:<10} "
            f"{len(t.verification.oracle_answer):>5}"
        )
        if curated and t.verification.oracle_tiers:
            tiers = dict(t.verification.oracle_tiers)
            req = sum(1 for v in tiers.values() if v == "required")
            sup = sum(1 for v in tiers.values() if v == "supplementary")
            ctx = sum(1 for v in tiers.values() if v == "context")
            click.echo(f"{base}  {req:>4}/{sup:>3}/{ctx:>3}")
        else:
            click.echo(base)

    click.echo()

    # Curation summary
    if curated:
        click.echo(f"Curation backends: {', '.join(curation_backends)}")
        click.echo()

    # Emit suite.toml for org-scale tasks
    from codeprobe.mining.writer import write_suite_manifest

    suite_path = write_suite_manifest(
        tasks_dir=tasks_dir,
        goal_name="org-scale comprehension",
        task_types=("org_scale_cross_repo",),
        description="Generated by codeprobe mine --org-scale",
    )

    _print_summary_block(
        task_count=len(tasks),
        quality_warning_count=0,
        tasks_dir=tasks_dir,
        suite_path=suite_path,
    )

    click.echo("Next steps:")
    click.echo()
    click.echo("  1. Validate task structure (offline sanity check):")
    click.echo(f"     codeprobe validate {tasks_dir}")
    click.echo()
    click.echo("  2. Run eval:")
    click.echo(f"     codeprobe run {repo_path} --agent claude")
    click.echo()
    click.echo("  3. Check individual oracle scores:")
    click.echo(f"     codeprobe oracle-check {tasks_dir}/<task_id>")
    click.echo()
    if curated:
        click.echo("  4. Weighted F1 scoring (curated tasks):")
        click.echo(
            f"     codeprobe oracle-check {tasks_dir}/<task_id> "
            f"--metric weighted_f1"
        )
        click.echo()


# ---------------------------------------------------------------------------
# Refresh-mode dispatch — re-mines a single task dir against a new commit.
# ---------------------------------------------------------------------------


def _resolve_refresh_commit(repo_path: Path) -> str:
    """Return the current HEAD SHA for ``repo_path``.

    Falls back to an empty string when the path isn't a git repo; the
    refresh flow itself will surface a clearer error in that case.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return ""


def _run_refresh(
    existing_task_dir: Path,
    *,
    repo_path_arg: str,
    accept_structural_change: bool,
) -> None:
    """Re-mine ``existing_task_dir`` against a new commit.

    The CLI wiring is intentionally thin. We:

    1. Read metadata.json + ground_truth.json from the existing task dir.
    2. Resolve the "new commit" from the repo path (HEAD).
    3. Build a fresh :class:`Task` from the on-disk ground truth (no
       live git mining — we only refresh the commit pointer and any
       file-set changes the user has already committed).
    4. Delegate fail-loud structural-mismatch handling to
       :func:`codeprobe.mining.refresh.refresh_task`.
    5. Write the refreshed task back with :func:`write_task_dir`.

    This is sufficient for the R20 contract (preserve-or-fail). More
    sophisticated flows — re-mining PR metadata, re-enriching via LLM —
    will layer on top of this primitive.
    """
    from codeprobe.mining.refresh import (
        StructuralMismatchError,
        read_structural_signature,
        read_task_metadata_json,
        refresh_task,
    )
    from codeprobe.mining.writer import write_task_dir
    from codeprobe.models.task import Task, TaskMetadata, TaskVerification

    existing_task_dir = Path(existing_task_dir).resolve()
    if not existing_task_dir.is_dir():
        raise click.UsageError(
            f"--refresh target is not a directory: {existing_task_dir}"
        )

    # 1. Read existing state. Missing metadata.json is a hard error — we
    #    refuse to refresh a task we can't identify.
    try:
        old_meta = read_task_metadata_json(existing_task_dir)
    except FileNotFoundError as exc:
        raise click.UsageError(str(exc)) from exc

    try:
        new_signature = read_structural_signature(existing_task_dir)
    except FileNotFoundError as exc:
        raise click.UsageError(str(exc)) from exc

    # 2. Resolve new commit.
    repo_path = _resolve_repo_path(repo_path_arg)
    new_commit = _resolve_refresh_commit(repo_path)
    if not new_commit:
        raise click.UsageError(
            f"Could not resolve HEAD commit in {repo_path}. "
            "Ensure --refresh is run against a git repository."
        )

    # 3. Build a new_task from the on-disk ground truth. The miner would
    #    normally supply a fresh Task, but for the primitive refresh case
    #    we pass through the existing structure and let refresh_task
    #    update the commit + history fields.
    old_metadata_section = old_meta.get("metadata") or {}
    old_verification = old_meta.get("verification") or {}
    language = old_metadata_section.get("language", "") or ""

    new_task = Task(
        id=str(old_meta.get("id", "")),
        repo=str(old_meta.get("repo", "")),
        metadata=TaskMetadata(
            name=str(old_metadata_section.get("name", "")),
            description=str(old_metadata_section.get("description", "")),
            language=language,
            category=str(old_metadata_section.get("category", "sdlc")),
            task_type=str(
                old_metadata_section.get("task_type", "sdlc_code_change")
            ),
            ground_truth_commit=new_commit,
            ground_truth_commit_history=tuple(
                old_metadata_section.get("ground_truth_commit_history") or ()
            ),
        ),
        verification=TaskVerification(
            type=str(old_verification.get("type", "test_script")),
            command=str(old_verification.get("command", "bash tests/test.sh")),
            verification_mode=str(
                old_verification.get("verification_mode", "test_script")
            ),
            oracle_type=new_signature.oracle_type,
            oracle_answer=new_signature.oracle_files,
        ),
    )

    # 4. Delegate to the pure function.
    try:
        result = refresh_task(
            existing_task_dir,
            new_task,
            new_commit,
            accept_structural_change=accept_structural_change,
        )
    except StructuralMismatchError as exc:
        click.echo(str(exc), err=True)
        sys.exit(2)

    # 5. Write the refreshed task back. write_task_dir uses the task.id
    #    (preserved) as the directory name, so this rewrites in place.
    base_dir = existing_task_dir.parent
    write_task_dir(result.task, base_dir, repo_path)

    if result.renumbered:
        click.echo(
            f"Refreshed (accepted structural change): {result.task.id} "
            f"-> history rooted at {new_commit[:7]}"
        )
    else:
        history = result.task.metadata.ground_truth_commit_history
        click.echo(
            f"Refreshed: {result.task.id} -> commit history "
            f"[{' -> '.join(c[:7] for c in history)}]"
        )
