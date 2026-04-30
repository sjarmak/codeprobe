"""``codeprobe calibrate-triad`` — score every task through null/golden/
adversarial fixtures and report which reward bands hold.

The triad is a property test for a task's scoring rubric. See
:mod:`codeprobe.calibration.triad` for the math; this module is the CLI
surface.

Usage::

    codeprobe calibrate-triad [TASK_DIR ...] \\
        [--out-dir docs/qa/codeprobe_calibration] \\
        [--report docs/qa/codeprobe_calibration_run_<date>.md] \\
        [--json | --no-json | --json-lines]

When ``TASK_DIR`` arguments are omitted, the command discovers tasks under
the in-repo defaults (``examples/``, ``.codeprobe/tasks``,
``e2e-codeprobe-self/tasks``, ``tests/fixtures``). Per-task JSON is always
written to ``--out-dir`` (default: ``docs/qa/codeprobe_calibration/``).

Exit codes:

* ``0`` — every task's bands held
* ``1`` — at least one band breach (the report still gets written)
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import click

from codeprobe.calibration.triad import (
    BAND_LIMITS,
    FAMILIES,
    TriadResult,
    discover_calibration_tasks,
    run_triad,
)
from codeprobe.cli._output_helpers import (
    add_json_flags,
    emit_envelope,
    resolve_mode,
)

_DEFAULT_OUT_DIR = Path("docs/qa/codeprobe_calibration")


@click.command("calibrate-triad")
@add_json_flags
@click.argument(
    "task_dirs",
    nargs=-1,
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
@click.option(
    "--out-dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=_DEFAULT_OUT_DIR,
    show_default=True,
    help="Directory for per-task JSON results.",
)
@click.option(
    "--report",
    type=click.Path(file_okay=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional Markdown report path. Defaults to "
    "docs/qa/codeprobe_calibration_run_<UTC-date>.md.",
)
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=None,
    help="Optional ceiling on the number of tasks to score (debug aid).",
)
@click.option(
    "--strict/--no-strict",
    default=True,
    show_default=True,
    help="Exit non-zero on any band breach. --no-strict reports breaches "
    "but always exits 0.",
)
def calibrate_triad(
    task_dirs: tuple[Path, ...],
    out_dir: Path,
    report: Path | None,
    limit: int | None,
    strict: bool,
    json_flag: bool,
    no_json_flag: bool,
    json_lines_flag: bool,
) -> None:
    """Run the null/golden/adversarial triad against each task.

    Always writes per-task JSON under ``--out-dir`` and a summary Markdown
    report at ``--report`` (or the default dated path). When ``--strict``
    is set (default), exits 1 if any task's reward bands break.
    """
    mode = resolve_mode(
        "calibrate-triad", json_flag, no_json_flag, json_lines_flag
    )

    tasks = _resolve_task_dirs(task_dirs)
    if limit is not None:
        tasks = tasks[:limit]

    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[TriadResult] = []
    for task_dir in tasks:
        if mode.mode == "pretty":
            click.echo(f"… {task_dir}", err=True)
        result = run_triad(task_dir)
        results.append(result)
        _write_task_json(out_dir, result)

    summary = _summarise(results)
    report_path = report or _default_report_path()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(_render_report(results, summary), encoding="utf-8")

    has_breach = any(not r.all_pass for r in results)
    exit_code = 1 if (strict and has_breach) else 0

    if mode.mode == "pretty":
        click.echo(_render_pretty_summary(results, summary, report_path))
        sys.exit(exit_code)

    emit_envelope(
        command="calibrate-triad",
        data={
            "task_count": len(results),
            "out_dir": str(out_dir),
            "report_path": str(report_path),
            "summary": summary,
            "results": [r.to_dict() for r in results],
        },
        ok=not has_breach,
        exit_code=exit_code,
    )
    sys.exit(exit_code)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_task_dirs(task_dirs: tuple[Path, ...]) -> list[Path]:
    """Expand explicit args or fall back to discovery defaults."""
    if task_dirs:
        expanded: list[Path] = []
        for td in task_dirs:
            if (td / "tests" / "test.sh").is_file() and (
                (td / "task.toml").is_file() or (td / "metadata.json").is_file()
            ):
                expanded.append(td)
                continue
            # Treat as a parent directory; discover within it.
            expanded.extend(discover_calibration_tasks([td]))
        # Deduplicate while preserving order.
        seen: set[Path] = set()
        unique: list[Path] = []
        for p in expanded:
            resolved = p.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            unique.append(p)
        return unique
    return discover_calibration_tasks()


def _write_task_json(out_dir: Path, result: TriadResult) -> None:
    """Persist one task's TriadResult as JSON in ``out_dir``."""
    target = out_dir / f"{result.task_id}.json"
    target.write_text(
        json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _summarise(results: list[TriadResult]) -> dict:
    """Compute per-fixture-family pass counts + breach clusters."""
    family_pass: Counter[str] = Counter()
    family_fail: Counter[str] = Counter()
    family_reward_sum: dict[str, float] = {f: 0.0 for f in FAMILIES}
    family_reward_count: dict[str, int] = {f: 0 for f in FAMILIES}

    breach_clusters: dict[str, list[str]] = {f: [] for f in FAMILIES}
    rubric_clusters: dict[str, list[str]] = {}

    task_errors: list[dict[str, str]] = []

    for r in results:
        if r.error:
            task_errors.append({"task_id": r.task_id, "error": r.error})
            continue
        for fx in r.fixtures:
            if fx.band_pass:
                family_pass[fx.family] += 1
            else:
                family_fail[fx.family] += 1
                breach_clusters[fx.family].append(r.task_id)
                key = f"{fx.family}|{fx.scorer_family or r.reward_type}"
                rubric_clusters.setdefault(key, []).append(r.task_id)
            family_reward_sum[fx.family] += fx.reward
            family_reward_count[fx.family] += 1

    family_summary: dict[str, dict] = {}
    for fam in FAMILIES:
        passed = family_pass[fam]
        failed = family_fail[fam]
        total = passed + failed
        rate = (passed / total) if total else 0.0
        avg_reward = (
            family_reward_sum[fam] / family_reward_count[fam]
            if family_reward_count[fam]
            else 0.0
        )
        family_summary[fam] = {
            "pass": passed,
            "fail": failed,
            "total": total,
            "pass_rate": round(rate, 4),
            "band": list(BAND_LIMITS[fam]),
            "mean_reward": round(avg_reward, 4),
        }

    return {
        "task_count": len(results),
        "task_errors": task_errors,
        "family_summary": family_summary,
        "breach_clusters": breach_clusters,
        "rubric_clusters": rubric_clusters,
    }


def _default_report_path() -> Path:
    """Default Markdown report path keyed to today's UTC date."""
    today = datetime.now(UTC).date().isoformat().replace("-", "_")
    return Path(f"docs/qa/codeprobe_calibration_run_{today}.md")


def _render_report(results: list[TriadResult], summary: dict) -> str:
    """Render the corpus run report as Markdown."""
    lines: list[str] = []
    today = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"# codeprobe calibration triad — corpus run ({today})\n")
    lines.append(
        "This report tabulates the null / golden / adversarial fixture "
        "scores against every task in the corpus. Each fixture is a "
        "synthetic agent output scored through the production scoring "
        "path. Band breaches mean the task's rubric does not enforce the "
        "expected reward range for that fixture.\n"
    )
    lines.append("## Reward bands\n")
    lines.append("| Fixture | Band (inclusive) | Intent |")
    lines.append("|---|---|---|")
    lines.append(
        "| null | ≤ 0.1 | empty agent output should not credit any task |"
    )
    lines.append(
        "| golden | ≥ 0.9 | the expected solution should max out the rubric |"
    )
    lines.append(
        "| adversarial | ≤ 0.5 | echoing oracle tokens with distractor noise "
        "should leave precision low enough that the headline reward is at "
        "most partial credit |"
    )
    lines.append("")

    lines.append("## Per-fixture pass rates\n")
    lines.append("| Fixture | Pass | Fail | Pass rate | Mean reward |")
    lines.append("|---|---|---|---|---|")
    for fam in FAMILIES:
        s = summary["family_summary"][fam]
        lines.append(
            f"| {fam} | {s['pass']} | {s['fail']} | "
            f"{s['pass_rate'] * 100:.1f}% | {s['mean_reward']:.3f} |"
        )
    lines.append("")

    if summary["task_errors"]:
        lines.append("## Task-level errors\n")
        lines.append("| Task | Error |")
        lines.append("|---|---|")
        for err in summary["task_errors"]:
            lines.append(f"| {err['task_id']} | {err['error']} |")
        lines.append("")

    lines.append("## Per-task table\n")
    lines.append(
        "| Task | reward_type | scorer_family | null | golden | adversarial | all_pass |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for r in sorted(results, key=lambda x: x.task_id):
        if r.error:
            lines.append(
                f"| {r.task_id} | — | — | ERR | ERR | ERR | ❌ ({r.error}) |"
            )
            continue
        cells: list[str] = []
        for fam in FAMILIES:
            fx = r.fixtures_by_family.get(fam)
            if fx is None:
                cells.append("—")
                continue
            mark = "✓" if fx.band_pass else "✗"
            cells.append(f"{mark} {fx.reward:.3f}")
        all_mark = "✓" if r.all_pass else "✗"
        sf = r.fixtures[0].scorer_family if r.fixtures else ""
        lines.append(
            f"| {r.task_id} | {r.reward_type} | {sf} | "
            + " | ".join(cells)
            + f" | {all_mark} |"
        )
    lines.append("")

    breach = summary["breach_clusters"]
    if any(breach[f] for f in FAMILIES):
        lines.append("## Breach clusters\n")
        for fam in FAMILIES:
            ids = breach[fam]
            if not ids:
                continue
            lo, hi = BAND_LIMITS[fam]
            lines.append(
                f"### {fam} (reward outside [{lo:.2f}, {hi:.2f}])\n"
            )
            lines.append(f"{len(ids)} task(s):\n")
            lines.extend(f"- `{t}`" for t in sorted(ids))
            lines.append("")

    if summary["rubric_clusters"]:
        lines.append("## Rubric clusters (fixture × scorer_family)\n")
        for key, ids in sorted(summary["rubric_clusters"].items()):
            family, scorer = key.split("|", 1)
            lines.append(
                f"- **{family} × {scorer}** → {len(ids)} breach(es): "
                + ", ".join(f"`{t}`" for t in sorted(ids)[:8])
                + (" …" if len(ids) > 8 else "")
            )
        lines.append("")

    return "\n".join(lines) + "\n"


def _render_pretty_summary(
    results: list[TriadResult], summary: dict, report_path: Path
) -> str:
    """Short, human-readable summary for stdout."""
    lines: list[str] = []
    lines.append(f"Calibration triad: {summary['task_count']} task(s)")
    for fam in FAMILIES:
        s = summary["family_summary"][fam]
        lines.append(
            f"  {fam:>11s}: {s['pass']}/{s['total']} pass "
            f"({s['pass_rate'] * 100:.1f}%) "
            f"mean reward {s['mean_reward']:.3f}"
        )
    breaches = sum(1 for r in results if not r.all_pass)
    lines.append(f"Tasks with band breaches: {breaches}")
    lines.append(f"Report: {report_path}")
    return "\n".join(lines)
