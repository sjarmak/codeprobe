"""Experiment directory I/O — create, save, load, checkpoint."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import asdict, fields, replace
from pathlib import Path

from codeprobe.analysis.stats import is_quota_casualty, is_scorable_run
from codeprobe.analysis.validity import is_infra_failure
from codeprobe.config.redact import redact_mcp_headers
from codeprobe.models.experiment import (
    CompletedTask as CompletedTask,
)
from codeprobe.models.experiment import (
    ConfigResults as ConfigResults,
)
from codeprobe.models.experiment import (
    Experiment as Experiment,
)
from codeprobe.models.experiment import (
    ExperimentConfig as ExperimentConfig,
)

logger = logging.getLogger(__name__)

_SAFE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def _validate_path_component(value: str, field: str) -> None:
    """Validate that *value* is a safe path component (no traversal)."""
    if not _SAFE_NAME.match(value):
        raise ValueError(
            f"Unsafe {field}: {value!r}. "
            "Use only letters, digits, hyphens, underscores, and dots."
        )


def create_experiment_dir(base_dir: Path, experiment: Experiment) -> Path:
    """Create the experiment directory structure and write experiment.json.

    Returns the experiment directory path.
    """
    _validate_path_component(experiment.name, "experiment name")
    for config in experiment.configs:
        _validate_path_component(config.label, "config label")

    exp_dir = base_dir / experiment.name
    (exp_dir / "tasks").mkdir(parents=True, exist_ok=True)

    # Ensure .codeprobe/ is excluded from git in the target repo.
    # base_dir is typically <repo>/.codeprobe — walk up to the repo root.
    from codeprobe.core.repo_hygiene import ensure_codeprobe_excluded

    if base_dir.name == ".codeprobe":
        ensure_codeprobe_excluded(base_dir.parent)

    for config in experiment.configs:
        (exp_dir / "runs" / config.label).mkdir(parents=True, exist_ok=True)

    save_experiment(exp_dir, experiment)
    return exp_dir


def find_experiment_candidates(repo_path: Path) -> list[Path]:
    """Return experiment directories discoverable under ``<repo>/.codeprobe``.

    Mirrors the run/interpret resolution order: a direct
    ``.codeprobe/experiment.json`` (the canonical location written by
    ``experiment init --non-interactive``) wins outright; otherwise every
    named subdirectory holding an ``experiment.json`` is a candidate,
    sorted for deterministic output.
    """
    codeprobe_dir = Path(repo_path) / ".codeprobe"
    if (codeprobe_dir / "experiment.json").is_file():
        return [codeprobe_dir]
    if not codeprobe_dir.is_dir():
        return []
    return sorted(
        child
        for child in codeprobe_dir.iterdir()
        if child.is_dir() and (child / "experiment.json").is_file()
    )


def ensure_default_experiment(
    repo_path: Path,
    *,
    description: str = "Auto-created by codeprobe",
) -> Path:
    """Return the repo's default experiment directory, creating it if absent.

    Resolution follows :func:`find_experiment_candidates`. When no experiment
    exists, a minimal ``default`` experiment is materialized at
    ``<repo>/.codeprobe/`` in the exact shape produced by
    ``codeprobe experiment init --non-interactive`` (experiment.json plus an
    empty ``tasks/`` directory, with ``.codeprobe/`` git-excluded).

    Raises ``ValueError`` when multiple named experiments exist — callers
    decide whether ambiguity is fatal (init) or a no-op (mine); this helper
    never guesses between experiments.
    """
    repo = Path(repo_path)
    codeprobe_dir = repo / ".codeprobe"
    candidates = find_experiment_candidates(repo)
    if len(candidates) > 1:
        raise ValueError(
            f"Multiple experiments found under {codeprobe_dir}: "
            + ", ".join(c.name for c in candidates)
        )
    if candidates:
        return candidates[0]

    from codeprobe.core.repo_hygiene import ensure_codeprobe_excluded

    codeprobe_dir.mkdir(parents=True, exist_ok=True)
    ensure_codeprobe_excluded(repo)
    save_experiment(
        codeprobe_dir,
        Experiment(name="default", description=description),
    )
    (codeprobe_dir / "tasks").mkdir(exist_ok=True)
    return codeprobe_dir


def record_task_ids(exp_dir: Path, task_ids: Sequence[str]) -> Experiment:
    """Union *task_ids* into the experiment's ``task_ids`` and persist it.

    ``run`` scopes task discovery to ``experiment.task_ids`` whenever the
    tuple is non-empty, so any command that writes task directories must
    record their ids here or the tasks are silently excluded. Union (not
    replace) so probe output coexists with previously mined tasks.

    Returns the updated (immutable) Experiment. Raises FileNotFoundError
    when *exp_dir* holds no experiment.json.
    """
    experiment = load_experiment(exp_dir)
    merged = tuple(sorted(set(experiment.task_ids) | set(task_ids)))
    updated = replace(experiment, task_ids=merged)
    save_experiment(exp_dir, updated)
    return updated


def save_experiment(exp_dir: Path, experiment: Experiment) -> None:
    """Write experiment.json to the experiment directory."""
    serialized_configs = []
    for c in experiment.configs:
        d = asdict(c)
        d["mcp_config"] = redact_mcp_headers(c.mcp_config)
        serialized_configs.append(d)
    data: dict = {
        "name": experiment.name,
        "description": experiment.description,
        "tasks_dir": experiment.tasks_dir,
        "configs": serialized_configs,
    }
    if experiment.task_ids:
        data["task_ids"] = list(experiment.task_ids)
    path = exp_dir / "experiment.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def load_experiment(exp_dir: Path) -> Experiment:
    """Load an Experiment from experiment.json.

    Raises FileNotFoundError if the directory or file doesn't exist.
    """
    path = exp_dir / "experiment.json"
    if not path.is_file():
        raise FileNotFoundError(f"Experiment not found: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        name = data["name"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise ValueError(f"Invalid experiment.json at {path}: {exc}") from exc

    # Lazy import: codeprobe.config.loader imports models.experiment which
    # imports this module — keep the coercion helper out of module init.
    from codeprobe.config.loader import _coerce_hide_local_source

    configs = [
        ExperimentConfig(
            label=c["label"],
            agent=c.get("agent", "claude"),
            model=c.get("model"),
            permission_mode=c.get("permission_mode", "default"),
            mcp_config=c.get("mcp_config"),
            allowed_tools=c.get("allowed_tools"),
            disallowed_tools=c.get("disallowed_tools"),
            mcp_mode=c.get("mcp_mode", "strict"),
            instruction_variant=c.get("instruction_variant"),
            preambles=tuple(c.get("preambles", ())),
            reward_type=c.get("reward_type", "binary"),
            max_turns=c.get("max_turns"),
            hide_local_source=_coerce_hide_local_source(
                c.get("hide_local_source")
            ),
            extra=c.get("extra", {}),
        )
        for c in data.get("configs", [])
    ]

    tasks_dir = data.get("tasks_dir", "tasks")
    # Validate all path components from the untrusted experiment.json
    _validate_path_component(name, "experiment name")
    _validate_path_component(tasks_dir, "tasks_dir")
    for c in configs:
        _validate_path_component(c.label, "config label")

    task_ids = tuple(data.get("task_ids", ()))

    return Experiment(
        name=name,
        description=data.get("description", ""),
        configs=configs,
        tasks_dir=tasks_dir,
        task_ids=task_ids,
    )


def _compute_summary(completed: Sequence[CompletedTask]) -> dict:
    """Compute aggregate summary from completed tasks.

    Follows the MCP-Eval-Tasks pattern: mean score, total cost, total tokens.
    Single-pass over the sequence.
    """
    if not completed:
        return {}

    n = 0
    # Score mean, duration total, and oracle metrics are over the scorable
    # reward population (non-executed status=="error" runs — quota casualties,
    # invalid-model/crash errors — excluded, matching is_scorable_run /
    # partition_reward_population, codeprobe-h3j4); cost/token totals stay over
    # all completed trials since errored attempts still cost real money. The
    # quota subset is surfaced via quota_error_count and the full excluded
    # count via errored_count. This loop interleaves both accumulations in a
    # single pass, so it keeps the inline predicate checks rather than calling
    # the partition helper (codeprobe-9jxx).
    reward_n = 0
    quota_count = 0
    infra_count = 0
    score_sum = 0.0
    cost_sum = 0.0
    has_cost = False
    input_sum = 0
    has_input = False
    output_sum = 0
    has_output = False
    cache_sum = 0
    has_cache = False
    cache_creation_sum = 0
    has_cache_creation = False
    dur_sum = 0.0
    # Optional oracle metrics (precision/recall/f1) — only present when the
    # scorer surfaced them via ScoreResult.details. Tasks without these
    # fields are excluded from the mean rather than counted as zero, so a
    # mixed batch (some oracle-scored, some not) reports the means honestly.
    metric_sums: dict[str, float] = {"precision": 0.0, "recall": 0.0, "f1": 0.0}
    metric_counts: dict[str, int] = {"precision": 0, "recall": 0, "f1": 0}

    for t in completed:
        n += 1
        if t.cost_usd is not None:
            cost_sum += t.cost_usd
            has_cost = True
        if t.input_tokens is not None:
            input_sum += t.input_tokens
            has_input = True
        if t.output_tokens is not None:
            output_sum += t.output_tokens
            has_output = True
        if t.cache_read_tokens is not None:
            cache_sum += t.cache_read_tokens
            has_cache = True
        if t.cache_creation_tokens is not None:
            cache_creation_sum += t.cache_creation_tokens
            has_cache_creation = True
        if is_quota_casualty(t):
            quota_count += 1
        if is_infra_failure(t):
            infra_count += 1
        if not is_scorable_run(t):
            continue
        reward_n += 1
        score_sum += t.automated_score
        dur_sum += t.duration_seconds
        details = t.scoring_details or {}
        for key in metric_sums:
            value = details.get(key)
            if isinstance(value, (int, float)):
                metric_sums[key] += float(value)
                metric_counts[key] += 1

    mean_score = score_sum / reward_n if reward_n else 0.0
    total_cost = cost_sum if has_cost else None

    summary: dict = {
        "tasks_completed": n,
        "quota_error_count": quota_count,
        "infra_failure_count": infra_count,
        "errored_count": n - reward_n,
        "mean_automated_score": round(mean_score, 4),
        "total_duration_seconds": round(dur_sum, 1),
        "total_tokens": {
            "input": input_sum if has_input else None,
            "output": output_sum if has_output else None,
            "cache_read": cache_sum if has_cache else None,
            "cache_creation": cache_creation_sum if has_cache_creation else None,
        },
        "total_cost_usd": round(total_cost, 6) if total_cost is not None else None,
    }

    for key in ("precision", "recall", "f1"):
        if metric_counts[key]:
            summary[f"mean_{key}"] = round(metric_sums[key] / metric_counts[key], 4)

    if total_cost and total_cost > 0 and reward_n:
        summary["score_per_dollar"] = round(mean_score / (total_cost / reward_n), 2)

    return summary


def save_config_results(
    exp_dir: Path,
    config_label: str,
    completed: Sequence[CompletedTask],
) -> Path:
    """Write results.json for a configuration.

    Includes a summary block with aggregate metrics (mean score, total cost,
    total tokens) following the MCP-Eval-Tasks pattern.

    Returns the path to the written file.
    """
    results_dir = exp_dir / "runs" / config_label
    results_dir.mkdir(parents=True, exist_ok=True)

    data = {
        "config": config_label,
        "completed": [asdict(t) for t in completed],
        "summary": _compute_summary(completed),
    }

    path = results_dir / "results.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def load_config_results(exp_dir: Path, config_label: str) -> ConfigResults:
    """Load results.json for a configuration.

    Raises FileNotFoundError if the results file doesn't exist.
    """
    path = exp_dir / "runs" / config_label / "results.json"
    if not path.is_file():
        raise FileNotFoundError(f"Results not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))

    # Generic over the dataclass fields so a field added to CompletedTask
    # can never be silently dropped on load (codeprobe-8up); unknown keys
    # from newer schemas are ignored, absent keys fall back to defaults.
    field_names = {f.name for f in fields(CompletedTask)}
    completed = [
        CompletedTask(**{k: v for k, v in t.items() if k in field_names})
        for t in data.get("completed", [])
    ]

    return ConfigResults(config=data["config"], completed=completed)


def append_checkpoint(checkpoint_path: Path, task: CompletedTask) -> None:
    """Append a completed task as a JSONL line to the checkpoint file.

    .. deprecated:: Use :class:`codeprobe.core.checkpoint.CheckpointStore` instead.
    """
    import warnings

    warnings.warn(
        "append_checkpoint is deprecated, use CheckpointStore.append()",
        DeprecationWarning,
        stacklevel=2,
    )
    entry = {"task_id": task.task_id, "automated_score": task.automated_score}
    with open(checkpoint_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def load_checkpoint(checkpoint_path: Path) -> set[str]:
    """Load completed task IDs from a JSONL checkpoint file.

    .. deprecated:: Use :class:`codeprobe.core.checkpoint.CheckpointStore` instead.

    Returns an empty set if the file doesn't exist. Skips malformed lines.
    """
    import warnings

    warnings.warn(
        "load_checkpoint is deprecated, use CheckpointStore.load_ids()",
        DeprecationWarning,
        stacklevel=2,
    )
    entries = load_checkpoint_entries(checkpoint_path)
    return {e["task_id"] for e in entries}


def load_checkpoint_entries(checkpoint_path: Path) -> list[dict]:
    """Load all checkpoint entries from a JSONL file.

    .. deprecated:: Use :class:`codeprobe.core.checkpoint.CheckpointStore` instead.

    Returns an empty list if the file doesn't exist. Skips malformed lines.
    """
    import warnings

    warnings.warn(
        "load_checkpoint_entries is deprecated, use CheckpointStore.load_entries()",
        DeprecationWarning,
        stacklevel=2,
    )
    if not checkpoint_path.is_file():
        return []

    entries: list[dict] = []
    with open(checkpoint_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if "task_id" in entry:
                    entries.append(entry)
            except json.JSONDecodeError:
                logger.warning("Skipping malformed checkpoint line: %s", line[:80])
    return entries
