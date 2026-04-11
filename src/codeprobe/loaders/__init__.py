"""Load task definitions from TOML or JSON files."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

from codeprobe.core.scoring import VALID_REWARD_TYPES
from codeprobe.models.task import Checkpoint, Task, TaskMetadata, TaskVerification


def load_task(path: Path) -> Task:
    """Load a Task from a TOML or JSON file, dispatching on extension.

    Raises:
        ValueError: If the file extension is unsupported or reward_type is unknown.
        KeyError: If required fields (id, repo) are missing.
    """
    suffix = path.suffix.lower()
    if suffix == ".toml":
        return _load_toml(path)
    if suffix == ".json":
        return _load_json(path)
    raise ValueError(
        f"Unsupported file extension: {suffix!r} (expected .toml or .json)"
    )


def _build_task(
    task_id: str,
    repo: str,
    meta: dict[str, Any],
    verif: dict[str, Any],
    time_limit_sec: int = 300,
    verification_modes: list[str] | None = None,
    checkpoints_raw: list[dict[str, Any]] | None = None,
) -> Task:
    """Build a Task from normalized dicts. Shared by TOML and JSON loaders."""
    reward_type = verif.get("reward_type", "binary")
    if reward_type not in VALID_REWARD_TYPES:
        raise ValueError(
            f"Unknown reward_type: {reward_type!r}. "
            f"Expected one of: {sorted(VALID_REWARD_TYPES)}"
        )

    tags_raw = meta.get("tags", ())
    if not isinstance(tags_raw, (list, tuple)):
        raise ValueError(f"'tags' must be a list, got {type(tags_raw).__name__}")

    modes_raw = verification_modes or ()
    if not isinstance(modes_raw, (list, tuple)):
        raise ValueError(
            f"'verification_modes' must be a list, got {type(modes_raw).__name__}"
        )

    valid_resource_tiers = {"light", "medium", "heavy"}
    resource_tier = meta.get("resource_tier", "medium")
    if resource_tier not in valid_resource_tiers:
        raise ValueError(
            f"Unknown resource_tier: {resource_tier!r}. "
            f"Expected one of: {sorted(valid_resource_tiers)}"
        )

    # Parse [[checkpoints]] into frozen Checkpoint dataclasses
    parsed_checkpoints: tuple[Checkpoint, ...] = ()
    if checkpoints_raw:
        parsed_checkpoints = tuple(
            Checkpoint(
                name=cp["name"],
                weight=float(cp["weight"]),
                verifier=cp["verifier"],
                description=cp.get("description", ""),
            )
            for cp in checkpoints_raw
        )

    return Task(
        id=task_id,
        repo=repo,
        metadata=TaskMetadata(
            name=meta.get("name", task_id),
            difficulty=meta.get("difficulty", "medium"),
            description=meta.get("description", ""),
            license=meta.get("license", ""),
            language=meta.get("language", ""),
            category=meta.get("category", "sdlc"),
            org_scale=meta.get("org_scale", False),
            mcp_suite=meta.get("mcp_suite"),
            task_type=meta.get("task_type", "sdlc_code_change"),
            tags=tuple(tags_raw),
            estimated_duration_sec=meta.get("estimated_duration_sec", 300),
            resource_tier=resource_tier,
        ),
        verification=TaskVerification(
            type=verif.get("type", "test_script"),
            command=verif.get("command", "bash tests/test.sh"),
            verification_mode=verif.get("verification_mode", "test_script"),
            reward_type=reward_type,
            scoring_policy=verif.get("scoring_policy", ""),
            weight_direct=float(verif.get("weight_direct", 0.5)),
            weight_artifact=float(verif.get("weight_artifact", 0.5)),
            checkpoints=parsed_checkpoints,
        ),
        time_limit_sec=time_limit_sec,
        verification_modes=tuple(modes_raw),
    )


def _load_toml(path: Path) -> Task:
    """Parse a TOML task.toml into a Task dataclass.

    Handles both CCX-style (with version key, mcp_suite, org_scale) and
    mined-style (with tags, test_ratio reward) formats.
    """
    with path.open("rb") as f:
        raw = tomllib.load(f)

    if "task" not in raw:
        raise ValueError(f"Missing required [task] section in {path}")
    task_sec = raw["task"]
    if "id" not in task_sec:
        raise ValueError(f"Missing required field 'id' in [task] section of {path}")
    meta_sec = raw.get("metadata", {})

    # TOML allows fields to appear in either [task] or [metadata]; merge with
    # metadata taking precedence for shared keys like difficulty/language/category.
    merged_meta = {
        "difficulty": task_sec.get("difficulty", "medium"),
        "language": task_sec.get("language", ""),
        "category": task_sec.get("category", "sdlc"),
        "org_scale": task_sec.get("org_scale", False),
        "mcp_suite": task_sec.get("mcp_suite"),
        "task_type": task_sec.get("task_type", "sdlc_code_change"),
        "tags": task_sec.get("tags", ()),
        "estimated_duration_sec": task_sec.get("estimated_duration_sec", 300),
        "resource_tier": task_sec.get("resource_tier", "medium"),
        **meta_sec,
    }

    return _build_task(
        task_id=task_sec["id"],
        repo=task_sec.get("repo", ""),
        meta=merged_meta,
        verif=raw.get("verification", {}),
        time_limit_sec=task_sec.get("time_limit_sec", 300),
        verification_modes=task_sec.get("verification_modes"),
        checkpoints_raw=raw.get("checkpoints"),
    )


def _load_json(path: Path) -> Task:
    """Parse a legacy metadata.json into a Task dataclass."""
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    return _build_task(
        task_id=raw["id"],
        repo=raw.get("repo", ""),
        meta=raw.get("metadata", {}),
        verif=raw.get("verification", {}),
        time_limit_sec=raw.get("time_limit_sec", 300),
        verification_modes=raw.get("verification_modes"),
    )
