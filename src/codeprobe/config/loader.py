"""Load .evalrc.yaml and convert to internal Experiment model."""

from __future__ import annotations

from dataclasses import replace
from itertools import product
from pathlib import Path
from typing import Literal, cast

import yaml

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
    2. If ``config.dimensions`` dict is non-empty, build cross-product.
    3. Otherwise, build a matrix from agents x models.
    4. If no models, one config per agent.
    """
    if config.configs:
        configs = _configs_from_explicit(config.configs)
    elif config.dimensions:
        configs = _configs_from_dimensions(config.dimensions, agent=config.agents[0])
    elif config.models:
        configs = _configs_from_matrix(config.agents, config.models)
    else:
        configs = [
            ExperimentConfig(label=agent, agent=agent) for agent in config.agents
        ]

    if config.max_turns is not None:
        configs = [
            replace(c, max_turns=config.max_turns) if c.max_turns is None else c
            for c in configs
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
    """Parse YAML string into a dict."""
    if not raw.strip():
        raise ValueError(f"Invalid .evalrc.yaml at {path}: file is empty")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid .evalrc.yaml at {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"Invalid .evalrc.yaml at {path}: expected a mapping, got {type(data).__name__}"
        )
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

    dimensions_raw = data.get("dimensions", {})
    if not isinstance(dimensions_raw, dict):
        dimensions_raw = {}

    max_turns_raw = data.get("max_turns")
    if max_turns_raw is not None:
        if not isinstance(max_turns_raw, int) or isinstance(max_turns_raw, bool) or max_turns_raw <= 0:
            raise ValueError(
                f"max_turns must be a positive integer, got {max_turns_raw!r}"
            )

    return EvalrcConfig(
        name=data.get("name", "default"),
        description=data.get("description", ""),
        tasks_dir=data.get("tasks_dir", "tasks"),
        agents=agents,
        models=models,
        configs=configs_raw,
        dimensions=dimensions_raw,
        max_turns=max_turns_raw,
    )


_HIDE_LOCAL_SOURCE_VALUES = frozenset({"off", "hide", "scaffold"})


def _coerce_hide_local_source(raw: object) -> Literal["off", "hide", "scaffold"]:
    """Map legacy bool / typed string into the new Literal field.

    Accepted forms:

    * ``True`` / ``False`` (legacy codeprobe-jf28 boolean) →
      ``"hide"`` / ``"off"``.
    * ``"off"`` / ``"hide"`` / ``"scaffold"`` (codeprobe-2nw2.4
      string form) → passed through unchanged.
    * Anything else raises ``ValueError`` so an experiment.json typo
      fails loud at load time rather than silently turning into
      ``"off"``.
    """
    if raw is None:
        return "off"
    if isinstance(raw, bool):
        return "hide" if raw else "off"
    if isinstance(raw, str):
        if raw in _HIDE_LOCAL_SOURCE_VALUES:
            return cast(Literal["off", "hide", "scaffold"], raw)
        raise ValueError(
            f"hide_local_source must be one of {sorted(_HIDE_LOCAL_SOURCE_VALUES)} "
            f"or a boolean; got {raw!r}"
        )
    raise ValueError(
        "hide_local_source must be a string in "
        f"{sorted(_HIDE_LOCAL_SOURCE_VALUES)} or a boolean; "
        f"got {type(raw).__name__}"
    )


def _coerce_unit_float(raw: object, field_name: str, default: float) -> float:
    """Coerce a user-supplied threshold, failing loud on bad input.

    Mirrors :func:`_coerce_hide_local_source`: several codeprobe-kdng
    threshold fields (``low_confidence_threshold``, the
    ``bias_overshipping_*`` trio) are read straight off untrusted
    experiment.json / .evalrc.yaml with no type or range check, so a
    quoted number (``"0.8"``) or a stray ``null`` loads silently and
    only blows up as a bare ``TypeError`` deep in scoring or bias
    detection — after the run has already paid full agent cost. ``None``
    (key absent, or explicitly ``null``) falls back to *default*, same
    as the hide_local_source coercion. Accepted values are plain
    ``int``/``float`` (``bool`` excluded — ``True``/``False`` are
    numerically 1/0 but not sensible thresholds) in the closed unit
    interval ``[0.0, 1.0]``, the scale of the confidence / recall /
    precision values these thresholds are compared against.
    """
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(
            f"{field_name} must be a number in [0.0, 1.0]; "
            f"got {raw!r} ({type(raw).__name__})"
        )
    value = float(raw)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{field_name} must be in [0.0, 1.0]; got {value!r}")
    return value


# Keys that map 1:1 onto ExperimentConfig fields.
_CONFIG_KEYS = frozenset(
    {
        "agent",
        "model",
        "permission_mode",
        "mcp_config",
        "allowed_tools",
        "disallowed_tools",
        "mcp_mode",
        "instruction_variant",
        "preambles",
        "reward_type",
        "max_turns",
        "hide_local_source",
        "low_confidence_threshold",
    }
)

# Keys consumed downstream via ExperimentConfig.extra (api.py, cli/run_cmd.py).
_EXTRA_PASSTHROUGH_KEYS = frozenset({"timeout_seconds"})


def _validated_extra(label: str, cfg: dict) -> dict:
    """Build the ``extra`` dict for a config entry, rejecting unknown keys.

    A typo like ``permision_mode`` must fail loud at load time instead of
    silently routing into ``extra`` and being ignored at run time. Keys the
    runtime genuinely reads from ``extra`` (``timeout_seconds``) pass through;
    anything else must be nested under an explicit ``extra:`` mapping.
    """
    unknown = set(cfg) - _CONFIG_KEYS - _EXTRA_PASSTHROUGH_KEYS - {"extra"}
    if unknown:
        raise ValueError(
            f"Config entry {label!r} has unknown key(s): {sorted(unknown)}. "
            f"Allowed keys: {sorted(_CONFIG_KEYS | _EXTRA_PASSTHROUGH_KEYS)}. "
            "To pass custom values through to the adapter, nest them under "
            "an explicit 'extra:' mapping."
        )

    extra_raw = cfg.get("extra", {})
    if not isinstance(extra_raw, dict):
        raise ValueError(
            f"Config entry {label!r}: 'extra' must be a mapping, "
            f"got {type(extra_raw).__name__}"
        )
    extra = dict(extra_raw)
    for key in _EXTRA_PASSTHROUGH_KEYS:
        if key in cfg:
            extra[key] = cfg[key]
    return extra


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
            allowed_tools=cfg.get("allowed_tools"),
            disallowed_tools=cfg.get("disallowed_tools"),
            mcp_mode=cfg.get("mcp_mode", "strict"),
            instruction_variant=cfg.get("instruction_variant"),
            preambles=tuple(cfg.get("preambles", ())),
            reward_type=cfg.get("reward_type", "binary"),
            max_turns=cfg.get("max_turns"),
            hide_local_source=_coerce_hide_local_source(
                cfg.get("hide_local_source")
            ),
            low_confidence_threshold=_coerce_unit_float(
                cfg.get("low_confidence_threshold"), "low_confidence_threshold", 0.5
            ),
            extra=_validated_extra(label, cfg),
        )
        for label, cfg in configs_dict.items()
    ]


def _configs_from_matrix(
    agents: list[str], models: list[str]
) -> list[ExperimentConfig]:
    """Build ExperimentConfig list from agents x models cross product."""
    return [
        ExperimentConfig(label=f"{agent}-{model}", agent=agent, model=model)
        for agent in agents
        for model in models
    ]


_DIMENSION_AXES = frozenset({"models", "tools", "prompts"})


def _configs_from_dimensions(
    dimensions: dict, agent: str = "claude"
) -> list[ExperimentConfig]:
    """Build ExperimentConfig list from cross-product of dimension axes.

    Supported axes:
      - ``models``: label → model ID string
      - ``tools``: label → MCP config dict (or None for no tools)
      - ``prompts``: label → instruction variant filename (str) or preamble
        names (list)

    The *agent* parameter sets the agent for all generated configs (defaults to
    the first agent in the evalrc). Axes with a single entry are omitted from
    the composite label.

    Raises ValueError for unknown axis names or duplicate labels.
    """
    unknown = set(dimensions) - _DIMENSION_AXES
    if unknown:
        raise ValueError(
            f"Unknown dimension axes: {unknown}. Allowed: {sorted(_DIMENSION_AXES)}"
        )

    axis_names = ("models", "tools", "prompts")
    axes: dict[str, dict] = {}
    for name in axis_names:
        axis = dimensions.get(name, {"default": None})
        if not isinstance(axis, dict):
            raise ValueError(
                f"dimensions.{name} must be a mapping, got {type(axis).__name__}"
            )
        axes[name] = axis

    # Only multi-valued axes contribute to the label
    multi = {name for name, ax in axes.items() if len(ax) > 1}

    configs: list[ExperimentConfig] = []
    for combo in product(*(axes[n].items() for n in axis_names)):
        labels = {n: combo[i][0] for i, n in enumerate(axis_names)}
        values = {n: combo[i][1] for i, n in enumerate(axis_names)}

        label_parts = [labels[n] for n in axis_names if n in multi]
        label = "-".join(label_parts) if label_parts else labels["models"]

        prompt_value = values["prompts"]
        instruction_variant = prompt_value if isinstance(prompt_value, str) else None
        preambles = tuple(prompt_value) if isinstance(prompt_value, list) else ()

        configs.append(
            ExperimentConfig(
                label=label,
                agent=agent,
                model=values["models"],
                mcp_config=(
                    values["tools"] if isinstance(values["tools"], dict) else None
                ),
                instruction_variant=instruction_variant,
                preambles=preambles,
            )
        )

    # Validate-or-die: labels must be unique
    seen_labels = [c.label for c in configs]
    if len(seen_labels) != len(set(seen_labels)):
        from collections import Counter

        dupes = [lbl for lbl, n in Counter(seen_labels).items() if n > 1]
        raise ValueError(f"dimensions produced duplicate config labels: {dupes}")

    return configs
