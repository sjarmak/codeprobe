"""codeprobe experiment — manage eval experiments."""

from __future__ import annotations

import json
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

from codeprobe.core.experiment import (
    create_experiment_dir,
    load_config_results,
    load_experiment,
    save_experiment,
)
from codeprobe.models.experiment import (
    Experiment,
    ExperimentConfig,
)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def experiment_init(
    path: str,
    name: str,
    description: str,
    non_interactive: bool = False,
) -> None:
    """Create a new experiment directory.

    In ``non_interactive`` mode the experiment is materialized inside the
    target's ``.codeprobe/`` directory so that ``.codeprobe/experiment.json``
    is the canonical default location, matching the documented golden path.
    """
    base_dir = Path(path)
    if non_interactive:
        # Default location is <path>/.codeprobe/, with experiment.json
        # written directly inside it (no nested name subdir).
        from codeprobe.core.experiment import _validate_path_component
        from codeprobe.core.repo_hygiene import ensure_codeprobe_excluded

        try:
            _validate_path_component(name, "experiment name")
        except ValueError as exc:
            click.echo(f"Error: {exc}", err=True)
            raise SystemExit(1)

        codeprobe_dir = base_dir / ".codeprobe"
        codeprobe_dir.mkdir(exist_ok=True)
        ensure_codeprobe_excluded(base_dir)

        exp_json = codeprobe_dir / "experiment.json"
        if exp_json.exists():
            click.echo(
                f"Error: experiment already exists at {exp_json}",
                err=True,
            )
            raise SystemExit(1)

        experiment = Experiment(name=name, description=description)
        try:
            save_experiment(codeprobe_dir, experiment)
        except ValueError as exc:
            click.echo(f"Error: {exc}", err=True)
            raise SystemExit(1)
        (codeprobe_dir / "tasks").mkdir(exist_ok=True)

        click.echo(f"Experiment '{name}' created at {codeprobe_dir}/")
        click.echo("  Tasks: 0 (add tasks to tasks/ directory)")
        click.echo(
            "  Configs: 0 (use 'codeprobe experiment add-config' to define "
            "configurations)"
        )
        return

    exp_dir = base_dir / name

    if exp_dir.exists():
        click.echo(f"Error: experiment '{name}' already exists at {exp_dir}", err=True)
        raise SystemExit(1)

    experiment = Experiment(
        name=name,
        description=description,
    )

    try:
        created = create_experiment_dir(base_dir, experiment)
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    click.echo(f"Experiment '{name}' created at {created}/")
    click.echo("  Tasks: 0 (add tasks to tasks/ directory)")
    click.echo(
        "  Configs: 0 (use 'codeprobe experiment add-config' to define configurations)"
    )


def _interactive_mcp_selection() -> str | None:
    """Offer interactive MCP config selection when available.

    Returns a file path string if the user selects a config, or None to skip.
    """
    from codeprobe.core.mcp_discovery import discover_mcp_configs

    discovered = discover_mcp_configs()
    if not discovered:
        return None

    click.echo()
    click.echo("Discovered MCP configurations:")
    for i, (p, servers) in enumerate(discovered, 1):
        click.echo(f"  {i}. {p}  ({len(servers)} servers)")
        for s in servers:
            click.echo(f"     - {s}")
    click.echo(f"  {len(discovered) + 1}. Skip (no MCP config)")
    click.echo()

    choice = click.prompt(
        "Select MCP config",
        type=click.IntRange(1, len(discovered) + 1),
        default=len(discovered) + 1,
    )
    if choice <= len(discovered):
        return str(discovered[choice - 1][0])
    return None


def experiment_add_config(
    path: str,
    label: str,
    agent: str,
    model: str | None,
    permission_mode: str,
    mcp_config_str: str | None,
    instruction_variant: str | None = None,
    preambles: tuple[str, ...] = (),
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    mcp_mode: str = "strict",
) -> None:
    """Add a configuration to an existing experiment."""
    exp_dir = Path(path)

    try:
        experiment = load_experiment(exp_dir)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    # Check for duplicate label
    existing_labels = [c.label for c in experiment.configs]
    if label in existing_labels:
        click.echo(
            f"Error: configuration '{label}' already exists in experiment '{experiment.name}'",
            err=True,
        )
        raise SystemExit(1)

    # Parse MCP config — offer interactive discovery when omitted in a TTY
    mcp_config: dict | None = None
    if mcp_config_str:
        try:
            mcp_config = json.loads(mcp_config_str)
        except json.JSONDecodeError:
            mcp_path = Path(mcp_config_str).expanduser().resolve()
            if mcp_path.is_file():
                mcp_config = json.loads(mcp_path.read_text(encoding="utf-8"))
            else:
                click.echo(
                    "Error: --mcp-config is not valid JSON or a file path",
                    err=True,
                )
                raise SystemExit(1)
    elif sys.stderr.isatty():
        mcp_config_str = _interactive_mcp_selection()
        if mcp_config_str:
            mcp_path = Path(mcp_config_str).expanduser().resolve()
            if mcp_path.is_file():
                mcp_config = json.loads(mcp_path.read_text(encoding="utf-8"))

    new_config = ExperimentConfig(
        label=label,
        agent=agent,
        model=model,
        permission_mode=permission_mode,
        mcp_config=mcp_config,
        instruction_variant=instruction_variant,
        preambles=preambles,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        mcp_mode=mcp_mode,
    )

    # Validate the label is a safe path component
    from codeprobe.core.experiment import _validate_path_component

    try:
        _validate_path_component(label, "config label")
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    updated = Experiment(
        name=experiment.name,
        description=experiment.description,
        configs=[*experiment.configs, new_config],
        tasks_dir=experiment.tasks_dir,
    )
    save_experiment(exp_dir, updated)

    # Create runs directory for this config
    (exp_dir / "runs" / label).mkdir(parents=True, exist_ok=True)

    click.echo(f"Configuration '{label}' added to experiment '{experiment.name}'")
    click.echo(f"  Agent: {agent}")
    click.echo(f"  Model: {model or '(not specified)'}")
    click.echo(f"  Total configs: {len(updated.configs)}")


def experiment_validate(
    path: str,
    *,
    allow_low_confidence: bool = False,
) -> None:
    """Validate experiment structure and readiness.

    By default, refuses tasks whose ``confidence.json#score`` is below the
    promotion gate (``mining.confidence.DEFAULT_THRESHOLD``). Pass
    ``allow_low_confidence=True`` to keep low-confidence tasks in the run
    plan — useful for exploratory experiments where reduced confidence is
    acceptable.
    """
    from codeprobe.mining.confidence import (
        DEFAULT_THRESHOLD,
        load_confidence_file,
        score_task_confidence,
    )

    exp_dir = Path(path)

    try:
        experiment = load_experiment(exp_dir)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    errors: list[str] = []
    warnings: list[str] = []

    # Discover tasks from the tasks directory
    tasks_dir = exp_dir / experiment.tasks_dir
    task_ids: list[str] = []
    if tasks_dir.is_dir():
        task_ids = sorted(
            d.name
            for d in tasks_dir.iterdir()
            if d.is_dir() and (d / "instruction.md").exists()
        )

    if not task_ids:
        errors.append("No tasks found. Add task directories to tasks/.")

    quarantined: list[str] = []
    for task_id in task_ids:
        task_dir = tasks_dir / task_id
        if not (task_dir / "tests" / "test.sh").exists():
            warnings.append(
                f"Task '{task_id}' has no tests/test.sh (automated scoring unavailable)"
            )
        # Confidence gate: prefer cached confidence.json, fall back to live score.
        score = load_confidence_file(task_dir)
        if score is None:
            score = score_task_confidence(task_dir)
        if score.score < DEFAULT_THRESHOLD:
            msg = (
                f"Task '{task_id}' confidence={score.score:.2f} "
                f"< {DEFAULT_THRESHOLD} (promotion gate)"
            )
            if allow_low_confidence:
                warnings.append(msg + " — admitted via --allow-low-confidence")
            else:
                quarantined.append(task_id)
                errors.append(
                    msg + " — quarantined; pass --allow-low-confidence to override"
                )

    if not experiment.configs:
        errors.append(
            "No configurations defined. Use 'add-config' to add at least one."
        )

    admitted_tasks = [t for t in task_ids if t not in quarantined]
    total_runs = len(admitted_tasks) * len(experiment.configs)

    click.echo(f"Experiment: {experiment.name}")
    click.echo(f"  Tasks: {len(task_ids)} ({len(quarantined)} quarantined)")
    click.echo(f"  Configurations: {len(experiment.configs)}")
    click.echo(f"  Total runs needed: {total_runs}")

    if errors:
        click.echo(f"\n  ERRORS ({len(errors)}):")
        for e in errors:
            click.echo(f"    - {e}")
    if warnings:
        click.echo(f"\n  WARNINGS ({len(warnings)}):")
        for w in warnings:
            click.echo(f"    - {w}")

    if not errors and not warnings:
        click.echo("\n  Status: READY to run")
    elif not errors:
        click.echo(f"\n  Status: READY (with {len(warnings)} warnings)")
    else:
        click.echo(f"\n  Status: NOT READY ({len(errors)} errors)")
        raise SystemExit(1)


def experiment_status(path: str) -> None:
    """Report completion status per configuration."""
    from codeprobe.mining.confidence import (
        confidence_histogram,
        load_confidence_file,
        score_task_confidence,
    )

    exp_dir = Path(path)

    try:
        experiment = load_experiment(exp_dir)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    tasks_dir = exp_dir / experiment.tasks_dir
    task_ids: list[str] = []
    if tasks_dir.is_dir():
        task_ids = sorted(
            d.name
            for d in tasks_dir.iterdir()
            if d.is_dir() and (d / "instruction.md").exists()
        )

    total_tasks = len(task_ids)

    click.echo(f"Experiment: {experiment.name}")
    click.echo(f"  Description: {experiment.description}")
    click.echo(f"  Tasks: {total_tasks}")

    # Confidence histogram across the task set
    if task_ids:
        scores = []
        for tid in task_ids:
            td = tasks_dir / tid
            cached = load_confidence_file(td)
            scores.append(cached if cached is not None else score_task_confidence(td))
        hist = confidence_histogram(scores)
        nonzero = {bucket: count for bucket, count in hist.items() if count}
        if nonzero:
            click.echo("  Confidence histogram:")
            for bucket, count in hist.items():
                bar = "#" * count
                click.echo(f"    {bucket:<10} {count:>4} {bar}")
    click.echo()

    if not experiment.configs:
        click.echo("  No configurations defined yet.")
        return

    click.echo(
        f"  {'Configuration':<25} {'Completed':<12} {'Score (avg)':<12} {'Status'}"
    )
    click.echo(f"  {'-' * 25} {'-' * 12} {'-' * 12} {'-' * 10}")

    for cfg in experiment.configs:
        completed = 0
        avg_score: float | None = None

        try:
            results = load_config_results(exp_dir, cfg.label)
            completed = len(results.completed)
            scores = [
                t.automated_score
                for t in results.completed
                if t.automated_score is not None
            ]
            if scores:
                avg_score = statistics.mean(scores)
        except FileNotFoundError:
            pass

        score_str = f"{avg_score:.2f}" if avg_score is not None else "--"
        status_str = (
            "complete" if completed == total_tasks and total_tasks > 0 else "pending"
        )
        progress = f"{completed}/{total_tasks}" if completed < total_tasks else "done"
        click.echo(f"  {cfg.label:<25} {progress:<12} {score_str:<12} {status_str}")


def experiment_aggregate(path: str, no_warn: bool = False) -> None:
    """Aggregate results across configurations into a comparison report.

    When ``no_warn`` is ``True``, bias warnings are suppressed in the
    console output and winner-suppression is disabled — useful for
    scripted aggregation. The structured ``bias_warnings`` array in
    ``aggregate.json`` is always written so downstream tooling still
    sees the signal.
    """
    exp_dir = Path(path)

    try:
        experiment = load_experiment(exp_dir)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    if len(experiment.configs) < 1:
        click.echo(
            "Error: need at least 1 configuration with results to aggregate",
            err=True,
        )
        raise SystemExit(1)

    # Load results for each config
    config_results: dict[str, list[dict]] = {}
    completed_by_config: dict[str, list[Any]] = {}
    for cfg in experiment.configs:
        try:
            results = load_config_results(exp_dir, cfg.label)
            completed_by_config[cfg.label] = list(results.completed)
            config_results[cfg.label] = [
                {
                    "task_id": t.task_id,
                    "automated_score": t.automated_score,
                    "duration_seconds": t.duration_seconds,
                    "cost_usd": t.cost_usd,
                    # Oracle metrics surfaced via scoring_details (Option 1
                    # plumbing: precision/recall/f1 don't change scoring,
                    # they just stop being hidden).
                    "scoring_details": dict(t.scoring_details or {}),
                }
                for t in results.completed
            ]
        except FileNotFoundError:
            completed_by_config[cfg.label] = []
            config_results[cfg.label] = []

    # Per-config summaries
    config_summaries: dict[str, dict] = {}
    for cfg_label, cfg_rows in config_results.items():
        scores = [
            r["automated_score"] for r in cfg_rows if r["automated_score"] is not None
        ]
        costs = [r["cost_usd"] for r in cfg_rows if r.get("cost_usd") is not None]
        times = [
            r["duration_seconds"]
            for r in cfg_rows
            if r.get("duration_seconds") is not None
        ]

        # Oracle metrics from scoring_details — only present for tasks scored
        # via the oracle (file_list / symbol_list / etc). Tasks without these
        # fields are excluded from the mean rather than counted as zero.
        def _detail_values(key: str) -> list[float]:
            out: list[float] = []
            for r in cfg_rows:
                v = (r.get("scoring_details") or {}).get(key)
                if isinstance(v, (int, float)):
                    out.append(float(v))
            return out

        precisions = _detail_values("precision")
        recalls = _detail_values("recall")
        f1s = _detail_values("f1")

        # ``mean_automated_score`` is the headline reward (recall-based for
        # IR scorers post-codeprobe-voxa). ``mean_reward`` is an alias for
        # callers who want an unambiguous name. ``ir_diagnostics`` carries
        # the F1 / precision / recall *measurements* (still computed, just
        # demoted from the headline so over-shipping doesn't fake a
        # capability-quality gap). See docs/scoring_model.md.
        mean_score = statistics.mean(scores) if scores else None
        mean_p = statistics.mean(precisions) if precisions else None
        mean_r = statistics.mean(recalls) if recalls else None
        mean_f = statistics.mean(f1s) if f1s else None
        config_summaries[cfg_label] = {
            "tasks_completed": len(cfg_rows),
            "mean_automated_score": mean_score,
            "mean_reward": mean_score,
            "stdev_automated_score": (
                statistics.stdev(scores) if len(scores) > 1 else None
            ),
            "total_cost_usd": sum(costs) if costs else None,
            "mean_cost_per_task": (statistics.mean(costs) if costs else None),
            "total_time_seconds": sum(times) if times else None,
            "score_per_dollar": (
                statistics.mean(scores) / statistics.mean(costs)
                if scores and costs and statistics.mean(costs) > 0
                else None
            ),
            # Back-compat: kept at the top level so older aggregate
            # consumers don't break. New code should read ir_diagnostics.
            "mean_precision": mean_p,
            "mean_recall": mean_r,
            "mean_f1": mean_f,
            "ir_diagnostics": {
                "mean_precision": mean_p,
                "mean_recall": mean_r,
                "mean_f1": mean_f,
            },
        }

    # Pairwise deltas
    config_labels = [c.label for c in experiment.configs]
    pairwise: list[dict] = []
    for i, a_label in enumerate(config_labels):
        for b_label in config_labels[i + 1 :]:
            a_scores = {
                r["task_id"]: r["automated_score"]
                for r in config_results.get(a_label, [])
                if r["automated_score"] is not None
            }
            b_scores = {
                r["task_id"]: r["automated_score"]
                for r in config_results.get(b_label, [])
                if r["automated_score"] is not None
            }
            shared = set(a_scores) & set(b_scores)
            if shared:
                deltas = [b_scores[t] - a_scores[t] for t in shared]
                mean_delta = statistics.mean(deltas)
                wins_b = sum(1 for d in deltas if d > 0.01)
                wins_a = sum(1 for d in deltas if d < -0.01)
                ties = len(deltas) - wins_b - wins_a

                cohens_d: float | None = None
                if len(deltas) > 1:
                    sd = statistics.stdev(deltas)
                    cohens_d = mean_delta / sd if sd > 0 else None

                pairwise.append(
                    {
                        "config_a": a_label,
                        "config_b": b_label,
                        "shared_tasks": len(shared),
                        "mean_delta": round(mean_delta, 4),
                        "wins_a": wins_a,
                        "wins_b": wins_b,
                        "ties": ties,
                        "cohens_d": (
                            round(cohens_d, 3) if cohens_d is not None else None
                        ),
                    }
                )

    # Bias detection — flag tautology and capability-boundary patterns.
    # Always computed so aggregate.json stays consistent across runs;
    # suppressed from stdout when --no-warn is set.
    from codeprobe.core.bias_detection import detect_bias_warnings

    bias_warnings, _ = detect_bias_warnings(experiment, exp_dir, config_results)
    suppress_winner = any(
        w.kind == "no_independent_baseline" for w in bias_warnings
    ) and not no_warn

    # Per-trial quality view derived from the typed CompletedTask records
    # (status, error_category, scoring_details) plus bias warnings. ZFC:
    # mechanical projection only, no semantic judgment.
    from codeprobe.analysis.trace_quality import TraceQualityReporter

    quality_reporter = TraceQualityReporter.from_completed_tasks(
        completed_by_config,
        bias_warnings=bias_warnings,
    )

    aggregate = {
        "experiment": experiment.name,
        "generated": _now_iso(),
        "config_count": len(experiment.configs),
        "config_summaries": config_summaries,
        "pairwise_deltas": pairwise,
        "bias_warnings": [w.to_dict() for w in bias_warnings],
        "quality_metrics": quality_reporter.to_dict(),
    }

    reports_dir = exp_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    out_path = reports_dir / "aggregate.json"
    out_path.write_text(json.dumps(aggregate, indent=2) + "\n", encoding="utf-8")

    # Print bias warnings prominently before the table so users see the
    # caveat in the same eyepath as the numbers. Severity-split (codeprobe-
    # 9re9): real tautology risks under "Bias warnings:"; signals that the
    # curator independently corroborated under "Informational:" so the
    # warnings panel only highlights actionable measurement bias.
    if bias_warnings and not no_warn:
        actionable = [
            w for w in bias_warnings
            if getattr(w, "severity", "warning") == "warning"
        ]
        informational = [
            w for w in bias_warnings
            if getattr(w, "severity", "warning") == "informational"
        ]
        if actionable:
            click.echo("Bias warnings:")
            for w in actionable:
                click.echo(f"  [{w.kind}] {w.message}")
            click.echo()
        if informational:
            click.echo("Informational:")
            for w in informational:
                click.echo(f"  [{w.kind}] {w.message}")
            click.echo()

    # Print summary table. Only render P/R columns if at least one config
    # exposed them — keeps the table compact for non-oracle experiments.
    has_pr = any(
        s.get("mean_precision") is not None or s.get("mean_recall") is not None
        for s in config_summaries.values()
    )
    click.echo(f"Experiment: {experiment.name}")
    if has_pr:
        click.echo(
            f"\n{'Configuration':<25} {'Score (auto)':<14} "
            f"{'Precision':<11} {'Recall':<8} "
            f"{'Cost/Task':<12} {'Score/$':<10}"
        )
        click.echo(
            f"{'-' * 25} {'-' * 14} "
            f"{'-' * 11} {'-' * 8} "
            f"{'-' * 12} {'-' * 10}"
        )
    else:
        click.echo(
            f"\n{'Configuration':<25} {'Score (auto)':<14} "
            f"{'Cost/Task':<12} {'Score/$':<10}"
        )
        click.echo(f"{'-' * 25} {'-' * 14} {'-' * 12} {'-' * 10}")

    if suppress_winner:
        # No independent baseline — show breakdown in declaration order
        # so readers can't infer a ranking from row position.
        ranked = list(config_summaries.items())
    else:
        ranked = sorted(
            config_summaries.items(),
            key=lambda x: x[1].get("mean_automated_score") or 0,
            reverse=True,
        )
    for label, s in ranked:
        auto = (
            f"{s['mean_automated_score']:.2f}"
            if s["mean_automated_score"] is not None
            else "--"
        )
        cost = (
            f"${s['mean_cost_per_task']:.2f}"
            if s["mean_cost_per_task"] is not None
            else "--"
        )
        spd = (
            f"{s['score_per_dollar']:.2f}"
            if s["score_per_dollar"] is not None
            else "--"
        )
        if has_pr:
            prec = (
                f"{s['mean_precision']:.2f}"
                if s.get("mean_precision") is not None
                else "--"
            )
            rec = (
                f"{s['mean_recall']:.2f}"
                if s.get("mean_recall") is not None
                else "--"
            )
            click.echo(
                f"{label:<25} {auto:<14} "
                f"{prec:<11} {rec:<8} "
                f"{cost:<12} {spd:<10}"
            )
        else:
            click.echo(f"{label:<25} {auto:<14} {cost:<12} {spd:<10}")

    if pairwise and not suppress_winner:
        click.echo("\nPairwise Comparisons:")
        for p in pairwise:
            click.echo(
                f"  {p['config_a']} vs {p['config_b']}: "
                f"delta={p['mean_delta']:+.3f}  "
                f"(wins: {p['wins_a']}/{p['wins_b']}/{p['ties']} A/B/tie)  "
                f"d={p['cohens_d'] or '--'}"
            )
    elif pairwise and suppress_winner:
        click.echo(
            "\nPairwise comparisons suppressed: see bias_warnings in aggregate.json."
        )

    click.echo(f"\nFull results: {out_path}")
