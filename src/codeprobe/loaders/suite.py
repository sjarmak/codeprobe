"""Load suite manifests from TOML files."""

from __future__ import annotations

import tomllib
from pathlib import Path

from codeprobe.models.suite import Suite
from codeprobe.models.task import TASK_TYPES


def load_suite(path: Path) -> Suite:
    """Parse a suite.toml into a Suite dataclass.

    Expected format::

        [suite]
        name = "navigation-benchmark"
        description = "Tasks focused on code navigation"
        task_dir = "tasks"
        task_types = ["micro_probe", "architecture_comprehension"]
        difficulties = ["easy", "medium"]
        eval_goal = "navigation"
        tags = ["python"]
        task_ids = []

    Raises:
        ValueError: If required fields are missing or task_types are invalid.
        FileNotFoundError: If the file does not exist.
    """
    if not path.exists():
        raise FileNotFoundError(f"Suite file not found: {path}")

    with path.open("rb") as f:
        raw = tomllib.load(f)

    if "suite" not in raw:
        raise ValueError(f"Missing required [suite] section in {path}")

    sec = raw["suite"]

    name = sec.get("name", "")
    if not name:
        raise ValueError(f"Missing required field 'name' in [suite] section of {path}")

    task_types_raw = sec.get("task_types", ())
    if isinstance(task_types_raw, str):
        task_types_raw = [task_types_raw]
    for tt in task_types_raw:
        if tt not in TASK_TYPES:
            raise ValueError(
                f"Unknown task_type: {tt!r}. " f"Expected one of: {sorted(TASK_TYPES)}"
            )

    difficulties_raw = sec.get("difficulties", ())
    if isinstance(difficulties_raw, str):
        difficulties_raw = [difficulties_raw]

    tags_raw = sec.get("tags", ())
    if isinstance(tags_raw, str):
        tags_raw = [tags_raw]

    task_ids_raw = sec.get("task_ids", ())
    if isinstance(task_ids_raw, str):
        task_ids_raw = [task_ids_raw]

    return Suite(
        name=name,
        description=sec.get("description", ""),
        task_dir=sec.get("task_dir", "tasks"),
        task_types=tuple(task_types_raw),
        difficulties=tuple(difficulties_raw),
        eval_goal=sec.get("eval_goal", ""),
        tags=tuple(tags_raw),
        task_ids=tuple(task_ids_raw),
    )
