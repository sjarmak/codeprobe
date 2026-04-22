"""In-process batch API for running experiments without CLI/subprocess overhead.

Provides ``run_experiment()`` — the programmatic entry point for notebooks,
scripts, and library consumers.  Returns a structured ``Report`` dataclass
from ``codeprobe.analysis``.

Key differences from the CLI (``codeprobe run``):

* No Click dependency — pure Python, no subprocess overhead.
* Returns structured ``Report`` data instead of printing to stdout.
* Uses ``CheckpointStore`` for durable resume across invocations.
* Can be called from Jupyter notebooks, scripts, or test harnesses.

Example::

    from pathlib import Path
    from codeprobe.api import run_experiment

    report = run_experiment(Path("./my-experiment"), max_cost_usd=5.0)
    for s in report.summaries:
        print(f"{s.config}: {s.pass_rate:.0%}")
"""

from __future__ import annotations

import logging
from pathlib import Path

from codeprobe.adapters.protocol import ALLOWED_PERMISSION_MODES, AgentConfig
from codeprobe.analysis.report import Report, generate_report
from codeprobe.analysis.stats import task_passed
from codeprobe.core.checkpoint import CheckpointStore
from codeprobe.core.executor import execute_config
from codeprobe.core.experiment import load_experiment, save_config_results
from codeprobe.core.registry import resolve
from codeprobe.models.experiment import ConfigResults, ExperimentConfig

logger = logging.getLogger(__name__)


def _build_experiment_config(raw: dict) -> ExperimentConfig:
    """Build an ExperimentConfig from a user-supplied dict.

    Validates required fields and provides sensible defaults.
    """
    if "label" not in raw:
        raise ValueError("Each config dict must include a 'label' key")

    return ExperimentConfig(
        label=raw["label"],
        agent=raw.get("agent", "claude"),
        model=raw.get("model"),
        permission_mode=raw.get("permission_mode", "default"),
        mcp_config=raw.get("mcp_config"),
        instruction_variant=raw.get("instruction_variant"),
        preambles=tuple(raw.get("preambles", ())),
        reward_type=raw.get("reward_type", "binary"),
        extra=raw.get("extra", {}),
    )


def _discover_task_dirs(
    tasks_dir: Path, *, task_ids: tuple[str, ...] = ()
) -> list[Path]:
    """Find valid task directories (those containing instruction.md).

    When *task_ids* is non-empty, only return tasks whose directory name
    appears in that tuple.
    """
    if not tasks_dir.is_dir():
        raise FileNotFoundError(f"Tasks directory not found: {tasks_dir}")

    if task_ids:
        allowed = set(task_ids)
        return sorted(
            d
            for d in tasks_dir.iterdir()
            if d.is_dir() and d.name in allowed and (d / "instruction.md").exists()
        )
    return sorted(
        d for d in tasks_dir.iterdir() if d.is_dir() and (d / "instruction.md").exists()
    )


def run_experiment(
    experiment_dir: Path,
    configs: list[dict] | None = None,
    max_cost_usd: float | None = None,
) -> Report:
    """Run an experiment in-process and return a structured Report.

    Parameters
    ----------
    experiment_dir:
        Path to the experiment directory (must contain ``experiment.json``
        and a ``tasks/`` subdirectory).
    configs:
        Optional list of config dicts to run.  Each dict must have a
        ``"label"`` key.  If *None*, uses configs from ``experiment.json``.
    max_cost_usd:
        Optional cost budget.  Execution halts when cumulative billable
        cost exceeds this amount.

    Returns
    -------
    Report
        A structured report with summaries, rankings, and pairwise
        comparisons for all configurations.

    Raises
    ------
    FileNotFoundError
        If *experiment_dir* or its ``experiment.json`` doesn't exist.
    ValueError
        If no task directories are found or a config is invalid.
    """
    experiment = load_experiment(experiment_dir)

    tasks_dir = experiment_dir / experiment.tasks_dir
    task_dirs = _discover_task_dirs(tasks_dir, task_ids=experiment.task_ids)

    if not task_dirs:
        raise ValueError(
            f"No tasks found in {tasks_dir}. "
            "Run 'codeprobe mine' or 'codeprobe scaffold' first."
        )

    # Resolve configs: explicit dicts > experiment.json > default
    if configs is not None:
        experiment_configs = [_build_experiment_config(c) for c in configs]
    elif experiment.configs:
        experiment_configs = list(experiment.configs)
    else:
        experiment_configs = [ExperimentConfig(label="default")]

    all_config_results: list[ConfigResults] = []

    for exp_config in experiment_configs:
        perm = exp_config.permission_mode
        if perm not in ALLOWED_PERMISSION_MODES:
            raise ValueError(
                f"Invalid permission_mode {perm!r} in config "
                f"{exp_config.label!r}. Allowed: {', '.join(sorted(ALLOWED_PERMISSION_MODES))}"
            )

        adapter = resolve(exp_config.agent)

        timeout = exp_config.extra.get("timeout_seconds", 3600)
        agent_config = AgentConfig(
            model=exp_config.model,
            permission_mode=perm,
            timeout_seconds=timeout,
            mcp_config=exp_config.mcp_config,
            allowed_tools=exp_config.allowed_tools,
            disallowed_tools=exp_config.disallowed_tools,
            cwd=str(experiment_dir.resolve()),
        )

        issues = adapter.preflight(agent_config)
        for issue in issues:
            logger.warning("[%s] Preflight: %s", exp_config.label, issue)

        # Set up checkpoint + runs dir
        config_runs_dir = experiment_dir / "runs" / exp_config.label
        config_runs_dir.mkdir(parents=True, exist_ok=True)

        legacy_jsonl = config_runs_dir / "checkpoint.jsonl"
        checkpoint_db = config_runs_dir / "checkpoint.db"
        checkpoint_store = CheckpointStore.from_legacy_path(
            legacy_jsonl, checkpoint_db, config_name=exp_config.label
        )

        logger.info("Running config %s (%d tasks)", exp_config.label, len(task_dirs))

        try:
            results = execute_config(
                adapter=adapter,
                task_dirs=task_dirs,
                repo_path=experiment_dir.resolve(),
                experiment_config=exp_config,
                agent_config=agent_config,
                checkpoint_store=checkpoint_store,
                runs_dir=config_runs_dir,
                max_cost_usd=max_cost_usd,
            )
        finally:
            checkpoint_store.close()

        save_config_results(experiment_dir, exp_config.label, results)

        passed = sum(1 for r in results if task_passed(r))
        mean = (
            sum(r.automated_score for r in results) / len(results) if results else 0.0
        )
        logger.info(
            "[%s] %d/%d passed (mean=%.2f)",
            exp_config.label,
            passed,
            len(results),
            mean,
        )

        all_config_results.append(
            ConfigResults(config=exp_config.label, completed=results)
        )

    return generate_report(experiment.name, all_config_results)
