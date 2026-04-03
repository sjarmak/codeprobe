"""codeprobe mine — extract eval tasks from repo history."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import click

# ---------------------------------------------------------------------------
# URL → local clone
# ---------------------------------------------------------------------------

_GIT_URL_PATTERN = re.compile(
    r"^(?:https?://|git@)"  # https:// or git@
    r"|^[\w.-]+/[\w.-]+$"  # owner/repo shorthand
)


def _is_git_url(path_or_url: str) -> bool:
    """Return True if the argument looks like a git URL or owner/repo shorthand."""
    return bool(_GIT_URL_PATTERN.match(path_or_url))


def _normalize_url(url: str) -> str:
    """Expand owner/repo shorthand to a full GitHub URL."""
    if "/" in url and not url.startswith(("https://", "http://", "git@")):
        return f"https://github.com/{url}.git"
    return url


def _clone_repo(url: str) -> Path:
    """Shallow-clone a repo into a temp directory. Returns the clone path.

    Uses ``--filter=blob:none`` for a fast treeless clone. The temp directory
    persists until the process exits (the user sees the path in output).
    """
    url = _normalize_url(url)
    # Derive a directory name from the URL
    repo_name = url.rstrip("/").rstrip(".git").rsplit("/", 1)[-1]
    clone_dir = Path(tempfile.mkdtemp(prefix=f"codeprobe-{repo_name}-"))

    click.echo(f"Cloning {url} → {clone_dir} ...")
    try:
        subprocess.run(
            ["git", "clone", "--filter=blob:none", url, str(clone_dir)],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as exc:
        click.echo(f"Clone failed: {(exc.stderr or '').strip()}")
        raise SystemExit(1) from exc
    except subprocess.TimeoutExpired:
        click.echo("Clone timed out after 120s.")
        raise SystemExit(1)

    click.echo(f"Cloned to {clone_dir}")
    return clone_dir


# ---------------------------------------------------------------------------
# Interactive workflow (mirrors mine-tasks skill phases 0–6)
# ---------------------------------------------------------------------------

_EVAL_GOALS = {
    "1": ("MCP / tool comparison", 6, "hard"),
    "2": ("Model comparison", 2, "mixed"),
    "3": ("Prompt / instruction comparison", 2, "mixed"),
    "4": ("General benchmarking", 0, "balanced"),
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


def _ask_eval_goal() -> tuple[str, int, str]:
    """Phase 0: Ask what the user is trying to learn."""
    click.echo()
    click.echo("What are you trying to learn?")
    click.echo(
        "  [1] MCP / tool comparison — harder tasks requiring cross-file navigation"
    )
    click.echo("  [2] Model comparison — mixed difficulty to find where models diverge")
    click.echo("  [3] Prompt / instruction comparison — variety of task types")
    click.echo("  [4] General benchmarking — balanced mix")
    click.echo()

    choice = click.prompt("Select goal", default="4", show_default=True)
    goal_name, min_files, bias = _EVAL_GOALS.get(choice, _EVAL_GOALS["4"])
    click.echo(f"  → {goal_name}")
    return goal_name, min_files, bias


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
    tasks: list["Task"],
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


def _show_results_table(tasks: list["Task"]) -> None:
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


def _show_next_steps(repo_path: Path, min_files: int) -> None:
    """Phase 6: Show next steps."""
    click.echo("Next steps:")
    click.echo()
    click.echo("  1. Review and enrich task instructions (recommended):")
    click.echo("     codeprobe mine {path} --enrich".format(path=repo_path))
    click.echo()
    click.echo("  2. Run the eval:")
    click.echo("     codeprobe run {path} --agent claude".format(path=repo_path))
    click.echo()
    click.echo("  3. Try a different model:")
    click.echo(
        "     codeprobe run {path} --agent claude --model claude-sonnet-4-6".format(
            path=repo_path,
        )
    )
    click.echo()
    click.echo("  4. Set a cost budget:")
    click.echo(
        "     codeprobe run {path} --agent claude --max-cost-usd 5.00".format(
            path=repo_path,
        )
    )
    click.echo()
    if min_files > 0:
        click.echo("  5. Mine more tasks for better confidence:")
        click.echo(
            "     codeprobe mine {path} --count 15 --min-files {mf}".format(
                path=repo_path,
                mf=min_files,
            )
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
# Main entry point
# ---------------------------------------------------------------------------


def run_mine(
    path: str,
    count: int = 5,
    source: str = "auto",
    min_files: int = 0,
    subsystems: tuple[str, ...] = (),
    discover_subsystems: bool = False,
    enrich: bool = False,
    interactive: bool | None = None,
    no_llm: bool = False,
    org_scale: bool = False,
    families: tuple[str, ...] = (),
) -> None:
    """Mine eval tasks from a repository.

    When *org_scale* is True, mines comprehension/IR tasks with oracle
    verification instead of SDLC code-change tasks.

    When *interactive* is True (default when TTY), runs the full interactive
    workflow matching the mine-tasks skill.

    By default, instruction.md is generated via LLM (Haiku) for quality.
    Pass *no_llm=True* (``--no-llm``) to skip LLM and use regex fallback.
    """
    from codeprobe.mining import generate_instructions, mine_tasks, write_task_dir

    # If path looks like a URL or owner/repo, clone it first
    if _is_git_url(path):
        repo_path = _clone_repo(path)
    else:
        repo_path = Path(path).resolve()
        if not repo_path.exists():
            click.echo(f"Path does not exist: {repo_path}")
            raise SystemExit(1)

    # Org-scale mining has a separate pipeline
    if org_scale:
        _run_org_scale_mine(repo_path, count=count, no_llm=no_llm, families=families)
        return

    # Determine interactive mode
    if interactive is None:
        interactive = _is_interactive()

    goal_name = "General benchmarking"
    bias = "balanced"

    if interactive:
        # Phase 0: Eval goal
        goal_name, goal_min_files, bias = _ask_eval_goal()
        if min_files == 0:
            min_files = goal_min_files

        # Phase 1: Mining configuration
        count = _ask_task_count()
        source = _ask_source()

        # Subsystem discovery: offer if not already specified
        if not subsystems and not discover_subsystems:
            if click.confirm("\nDiscover and filter by subsystems?", default=False):
                discover_subsystems = True

    # Subsystem discovery
    if discover_subsystems:
        subsystems = _discover_and_select(repo_path, source)
        if not subsystems:
            return

    # Normalize prefixes to end with /
    subsystems = tuple(s if s.endswith("/") else s + "/" for s in subsystems)

    if interactive:
        # Phase 2: Pre-flight summary
        if not _show_preflight(
            repo_path, goal_name, count, source, min_files, bias, subsystems
        ):
            click.echo("Aborted.")
            return

    # Phase 3: Run mining
    if interactive:
        click.echo()
        click.echo("Mining tasks...")

    mine_result = mine_tasks(
        repo_path,
        count=count,
        source_hint=source,
        min_files=min_files,
        subsystems=subsystems,
    )
    tasks = mine_result.tasks

    if not tasks:
        click.echo(
            "No suitable tasks found. Try a repo with merged PRs that include tests."
        )
        return

    # LLM instruction generation (default) or legacy enrichment
    use_llm = not no_llm
    if use_llm:
        from codeprobe.core.llm import llm_available

        if llm_available():
            click.echo("Generating instructions via LLM...")
            tasks = generate_instructions(
                tasks,
                pr_bodies=mine_result.pr_bodies,
                changed_files_map=mine_result.changed_files_map,
            )
        else:
            click.echo(
                "No LLM backend available — using regex fallback for instructions.\n"
                "Install an LLM backend for better quality: "
                "pip install codeprobe[anthropic]"
            )
    elif enrich:
        # Legacy --enrich flag: only enrich low-quality tasks
        from codeprobe.mining.extractor import enrich_tasks

        click.echo("Enriching low-quality tasks via LLM...")
        tasks = enrich_tasks(tasks)

    # Clear stale tasks from prior runs before writing new ones
    tasks_dir = repo_path / ".codeprobe" / "tasks"
    if tasks_dir.exists():
        shutil.rmtree(tasks_dir)

    for task in tasks:
        write_task_dir(task, tasks_dir, repo_path)

    # Phase 5: Results table
    _show_results_table(tasks)

    # Phase 4: Quality review
    warnings = _quality_review(tasks, goal_name, bias)
    if warnings:
        click.echo("Quality warnings:")
        for w in warnings:
            click.echo(f"  ! {w}")
        click.echo()

    click.echo(f"Tasks written to {tasks_dir}")
    if subsystems:
        click.echo(f"Subsystems: {', '.join(subsystems)}")
    click.echo()

    # Phase 6: Next steps
    _show_next_steps(repo_path, min_files)


# ---------------------------------------------------------------------------
# Org-scale mining pipeline
# ---------------------------------------------------------------------------


def _run_org_scale_mine(
    repo_path: Path,
    *,
    count: int = 5,
    no_llm: bool = False,
    families: tuple[str, ...] = (),
) -> None:
    """Mine org-scale comprehension tasks with oracle verification."""
    from codeprobe.mining.org_scale import mine_org_scale_tasks
    from codeprobe.mining.org_scale_families import FAMILY_BY_NAME, TaskFamily
    from codeprobe.mining.writer import write_task_dir

    click.echo(f"Scanning {repo_path.name} for org-scale patterns...", err=True)

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

    result = mine_org_scale_tasks(
        repo_path,
        count=count,
        families=selected_families,
        no_llm=no_llm,
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

    # Write tasks
    tasks_dir = repo_path / ".codeprobe" / "tasks"
    if tasks_dir.exists():
        shutil.rmtree(tasks_dir)

    for task in result.tasks:
        write_task_dir(task, tasks_dir, repo_path)

    # Results table
    click.echo(f"Generated {len(result.tasks)} org-scale tasks:")
    click.echo()
    click.echo(
        f"  {'#':>2}  {'Task ID':<14} {'Family':<24} {'Difficulty':<10} "
        f"{'Multi-hop':>9}  {'Files':>5}"
    )
    click.echo("  " + "-" * 70)
    for i, t in enumerate(result.tasks, 1):
        is_mh = (
            "yes"
            if t.id.endswith("mh") or len(t.verification.oracle_answer) > 0
            else "no"
        )
        click.echo(
            f"  {i:>2}  {t.id:<14} {t.metadata.category:<24} "
            f"{t.metadata.difficulty:<10} {is_mh:>9}  "
            f"{len(t.verification.oracle_answer):>5}"
        )
    click.echo()

    click.echo(f"Tasks written to {tasks_dir}")
    click.echo()
    click.echo("Next steps:")
    click.echo(f"  1. Run eval:     codeprobe run {repo_path} --agent claude")
    click.echo(f"  2. Check scores: codeprobe oracle-check {tasks_dir}/<task_id>")
    click.echo()
