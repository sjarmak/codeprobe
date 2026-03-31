"""Write .evalrc.yaml configuration files."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from codeprobe.models.evalrc import EvalrcConfig


def write_evalrc(target_dir: Path, config: EvalrcConfig) -> Path:
    """Serialize an EvalrcConfig to .evalrc.yaml in *target_dir*.

    Tries PyYAML first; falls back to manual YAML for the flat schema.
    Returns the path to the written file.
    """
    data = _strip_defaults(asdict(config))
    path = target_dir / ".evalrc.yaml"
    path.write_text(_to_yaml(data), encoding="utf-8")
    return path


_EMPTY = (None, "", [], {})


def _strip_defaults(data: dict) -> dict:
    """Remove keys whose values are empty/None or match EvalrcConfig defaults."""
    return {
        key: value
        for key, value in data.items()
        if value not in _EMPTY
        and not (key == "tasks_dir" and value == "tasks")
    }


def _to_yaml(data: dict) -> str:
    """Convert a flat dict to YAML string."""
    try:
        import yaml

        return yaml.dump(data, default_flow_style=False, sort_keys=False)
    except ImportError:
        return _manual_yaml(data)


def _manual_yaml(data: dict) -> str:
    """Produce YAML for the flat EvalrcConfig schema without PyYAML."""
    lines: list[str] = []
    for key, value in data.items():
        if isinstance(value, list):
            items = ", ".join(str(v) for v in value)
            lines.append(f"{key}: [{items}]")
        elif isinstance(value, dict):
            lines.append(f"{key}:")
            for k, v in value.items():
                lines.append(f"  {k}: {v}")
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines) + "\n"
