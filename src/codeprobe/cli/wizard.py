"""Goal-specific questionnaire functions for codeprobe init."""

from __future__ import annotations

import json
import re
from pathlib import Path

import click

from codeprobe.models.evalrc import EvalrcConfig
from codeprobe.models.experiment import ExperimentConfig

_SAFE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def ask_mcp_comparison(
    *,
    experiment_name: str,
    agent: str,
    model: str | None,
    mcp_config_path: str,
) -> tuple[EvalrcConfig, list[ExperimentConfig]]:
    """Goal 1: Compare baseline agent vs MCP-augmented agent."""
    mcp_data = _load_json(mcp_config_path)

    baseline = ExperimentConfig(label="baseline", agent=agent, model=model)
    with_mcp = ExperimentConfig(
        label="with-mcp", agent=agent, model=model, mcp_config=mcp_data
    )

    evalrc = EvalrcConfig(name=experiment_name, agents=[agent])
    return evalrc, [baseline, with_mcp]


def ask_model_comparison(
    *,
    experiment_name: str,
    agent: str,
    models: list[str],
) -> tuple[EvalrcConfig, list[ExperimentConfig]]:
    """Goal 2: Compare different models."""
    configs = [
        ExperimentConfig(label=model, agent=agent, model=model) for model in models
    ]
    evalrc = EvalrcConfig(name=experiment_name, agents=[agent], models=models)
    return evalrc, configs


def ask_prompt_comparison(
    *,
    experiment_name: str,
    agent: str,
    model: str | None,
    variants: list[str],
) -> tuple[EvalrcConfig, list[ExperimentConfig]]:
    """Goal 3: Compare different prompts or instruction styles."""
    configs = [
        ExperimentConfig(
            label=Path(v).stem,
            agent=agent,
            model=model,
            instruction_variant=v,
        )
        for v in variants
    ]
    evalrc = EvalrcConfig(name=experiment_name, agents=[agent])
    return evalrc, configs


def ask_custom(
    *,
    experiment_name: str,
    configs: list[dict],
) -> tuple[EvalrcConfig, list[ExperimentConfig]]:
    """Goal 4: Custom comparison."""
    agents_seen: set[str] = set()
    experiment_configs: list[ExperimentConfig] = []

    for entry in configs:
        agent = entry.get("agent", "claude")
        agents_seen.add(agent)
        mcp_path = entry.get("mcp_config_path")
        mcp_data = _load_json(mcp_path) if mcp_path else None

        experiment_configs.append(
            ExperimentConfig(
                label=entry["label"],
                agent=agent,
                model=entry.get("model"),
                mcp_config=mcp_data,
                instruction_variant=entry.get("instruction_variant"),
            )
        )

    evalrc = EvalrcConfig(name=experiment_name, agents=sorted(agents_seen))
    return evalrc, experiment_configs


def validate_experiment_name(name: str) -> str:
    """Validate that *name* is safe for use as a directory name."""
    if not _SAFE_NAME.match(name):
        raise click.BadParameter(
            f"Invalid experiment name: {name!r}. "
            "Use only letters, digits, hyphens, underscores, and dots."
        )
    return name


def _load_json(path: str) -> dict:
    """Load and return a JSON file as a dict."""
    p = Path(path)
    if not p.is_file():
        raise click.BadParameter(f"File not found: {path}")
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise click.BadParameter(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise click.BadParameter(f"Expected a JSON object in {path}, got {type(data).__name__}")
    return data
