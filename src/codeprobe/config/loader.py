"""Load .evalrc.yaml and convert to internal Experiment model."""

from __future__ import annotations

from pathlib import Path

from codeprobe.models.evalrc import EvalrcConfig
from codeprobe.models.experiment import Experiment, ExperimentConfig

_CANDIDATES = (".evalrc.yaml", ".evalrc.yml")


def load_evalrc(directory: Path) -> EvalrcConfig:
    """Load .evalrc.yaml (or .evalrc.yml) from *directory*.

    Raises FileNotFoundError if neither file exists.
    Raises ValueError if the YAML is invalid or empty.
    """
    path = _find_evalrc(directory)
    raw = path.read_text(encoding="utf-8")
    data = _parse_yaml(raw, path)
    return _to_evalrc(data)


def to_experiment(config: EvalrcConfig) -> Experiment:
    """Convert an EvalrcConfig into an Experiment with resolved configs.

    Config resolution order:
    1. If ``config.configs`` dict is non-empty, use those explicitly.
    2. Otherwise, build a matrix from agents x models.
    3. If no models, one config per agent.
    """
    if config.configs:
        configs = _configs_from_explicit(config.configs)
    elif config.models:
        configs = _configs_from_matrix(config.agents, config.models)
    else:
        configs = [
            ExperimentConfig(label=agent, agent=agent)
            for agent in config.agents
        ]

    return Experiment(
        name=config.name,
        description=config.description,
        configs=configs,
        tasks_dir=config.tasks_dir,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_evalrc(directory: Path) -> Path:
    """Return the first existing evalrc path, preferring .yaml over .yml."""
    for name in _CANDIDATES:
        path = directory / name
        if path.is_file():
            return path
    raise FileNotFoundError(
        f"No .evalrc.yaml found in {directory}. Run 'codeprobe init' first."
    )


def _parse_yaml(raw: str, path: Path) -> dict:
    """Parse YAML string into a dict, falling back to manual parsing."""
    if not raw.strip():
        raise ValueError(f"Invalid .evalrc.yaml at {path}: file is empty")
    try:
        import yaml

        data = yaml.safe_load(raw)
    except ImportError:
        data = _manual_parse(raw)
    except Exception as exc:
        raise ValueError(f"Invalid .evalrc.yaml at {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Invalid .evalrc.yaml at {path}: expected a mapping, got {type(data).__name__}")
    return data


def _manual_parse(raw: str) -> dict:
    """Minimal YAML-subset parser for flat key: value pairs and lists."""
    data: dict = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            items = [v.strip().strip("\"'") for v in value[1:-1].split(",") if v.strip()]
            data[key] = items
        elif value:
            data[key] = value
    return data


def _to_evalrc(data: dict) -> EvalrcConfig:
    """Map a raw dict to an EvalrcConfig, applying defaults."""
    agents = data.get("agents", ["claude"])
    if isinstance(agents, str):
        agents = [agents]

    models = data.get("models", [])
    if isinstance(models, str):
        models = [models]

    configs_raw = data.get("configs", {})
    if not isinstance(configs_raw, dict):
        configs_raw = {}

    return EvalrcConfig(
        name=data.get("name", "default"),
        description=data.get("description", ""),
        tasks_dir=data.get("tasks_dir", "tasks"),
        agents=agents,
        models=models,
        configs=configs_raw,
    )


def _configs_from_explicit(configs_dict: dict) -> list[ExperimentConfig]:
    """Build ExperimentConfig list from explicit configs mapping."""
    for label, cfg in configs_dict.items():
        if not isinstance(cfg, dict):
            raise ValueError(
                f"Config entry {label!r} must be a mapping, got {type(cfg).__name__}"
            )
    return [
        ExperimentConfig(
            label=label,
            agent=cfg.get("agent", "claude"),
            model=cfg.get("model"),
            permission_mode=cfg.get("permission_mode", "default"),
            mcp_config=cfg.get("mcp_config"),
            instruction_variant=cfg.get("instruction_variant"),
            extra={k: v for k, v in cfg.items() if k not in {
                "agent", "model", "permission_mode", "mcp_config", "instruction_variant",
            }},
        )
        for label, cfg in configs_dict.items()
    ]


def _configs_from_matrix(agents: list[str], models: list[str]) -> list[ExperimentConfig]:
    """Build ExperimentConfig list from agents x models cross product."""
    return [
        ExperimentConfig(label=f"{agent}-{model}", agent=agent, model=model)
        for agent in agents
        for model in models
    ]
