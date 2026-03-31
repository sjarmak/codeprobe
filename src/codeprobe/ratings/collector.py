"""Collect and store micro-ratings for coding agent sessions.

Appends rating records to a JSONL file. Each record captures the user's
subjective quality rating alongside a snapshot of the active Claude Code
configuration (model, MCPs, skills) and basic session metadata.

All data stays local — nothing is transmitted externally.
"""

from __future__ import annotations

import fcntl
import json
import os
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_RATINGS_PATH = Path("ratings.jsonl")


@dataclass(frozen=True)
class ConfigSnapshot:
    """Immutable snapshot of the active Claude Code configuration."""

    model: str | None = None
    mcps: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "mcps": list(self.mcps),
            "skills": list(self.skills),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConfigSnapshot:
        return cls(
            model=data.get("model"),
            mcps=tuple(data.get("mcps") or []),
            skills=tuple(data.get("skills") or []),
        )


@dataclass(frozen=True)
class RatingRecord:
    """Immutable record of a single session rating."""

    ts: str
    rating: int
    config: ConfigSnapshot
    task_type: str = ""
    duration_s: float | None = None
    tool_calls: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "rating": self.rating,
            "config": self.config.to_dict(),
            "task_type": self.task_type,
            "duration_s": self.duration_s,
            "tool_calls": self.tool_calls,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RatingRecord:
        return cls(
            ts=data.get("ts", ""),
            rating=int(data.get("rating", 0)),
            config=ConfigSnapshot.from_dict(data.get("config") or {}),
            task_type=data.get("task_type", ""),
            duration_s=data.get("duration_s"),
            tool_calls=data.get("tool_calls"),
        )


def snapshot_config() -> ConfigSnapshot:
    """Read current Claude Code configuration from settings files.

    Reads from ~/.claude/settings.json and ~/.claude.json, extracting the
    active model, enabled MCPs, and installed skills. Returns partial data
    if files are missing or malformed.
    """
    model: str | None = None
    mcps: list[str] = []
    skills: list[str] = []

    settings_path = Path.home() / ".claude" / "settings.json"
    if settings_path.is_file():
        try:
            settings = json.loads(settings_path.read_text())
            model = settings.get("model") or settings.get("defaultModel")
            mcp_servers = settings.get("mcpServers") or {}
            mcps = sorted(mcp_servers.keys())
        except (json.JSONDecodeError, OSError):
            pass

    claude_json = Path.home() / ".claude.json"
    if claude_json.is_file():
        try:
            data = json.loads(claude_json.read_text())
            if model is None:
                model = data.get("model")
            if not mcps:
                mcp_servers = data.get("mcpServers") or {}
                mcps = sorted(mcp_servers.keys())
        except (json.JSONDecodeError, OSError):
            pass

    skills_dir = Path.home() / ".claude" / "skills"
    if skills_dir.is_dir():
        skills = sorted(
            d.name
            for d in skills_dir.iterdir()
            if d.is_dir() and (d / "SKILL.md").is_file()
        )

    return ConfigSnapshot(
        model=model,
        mcps=tuple(mcps),
        skills=tuple(skills),
    )


def record_rating(
    rating: int,
    config_snapshot: ConfigSnapshot | None = None,
    session_metadata: dict[str, Any] | None = None,
    path: Path = DEFAULT_RATINGS_PATH,
) -> RatingRecord:
    """Append a rating record to the JSONL file.

    Uses file locking for atomic appends.

    Args:
        rating: Quality score from 1 (poor) to 5 (excellent).
        config_snapshot: Active config; auto-detected if None.
        session_metadata: Optional dict with task_type, duration_s, tool_calls.
        path: Path to the JSONL ratings file.

    Returns:
        The RatingRecord that was written.

    Raises:
        ValueError: If rating is not in range 1-5.
    """
    if not 1 <= rating <= 5:
        raise ValueError(f"Rating must be 1-5, got {rating}")

    if config_snapshot is None:
        config_snapshot = snapshot_config()

    meta = session_metadata or {}
    record = RatingRecord(
        ts=datetime.now(timezone.utc).isoformat(),
        rating=rating,
        config=config_snapshot,
        task_type=str(meta.get("task_type", "")),
        duration_s=meta.get("duration_s"),
        tool_calls=meta.get("tool_calls"),
    )

    line = json.dumps(record.to_dict(), separators=(",", ":")) + "\n"

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, line.encode("utf-8"))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    return record


def load_ratings(path: Path = DEFAULT_RATINGS_PATH) -> list[RatingRecord]:
    """Load all rating records from a JSONL file.

    Skips malformed lines silently to preserve good data.
    """
    path = Path(path)
    if not path.is_file():
        return []

    records: list[RatingRecord] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            records.append(RatingRecord.from_dict(data))
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return records


@dataclass(frozen=True)
class DimensionStats:
    """Statistics for a single config dimension value."""

    dimension: str
    value: str
    count: int
    mean: float
    stdev: float | None
    median: float


def summarize(ratings: list[RatingRecord]) -> dict[str, list[DimensionStats]]:
    """Compute average ratings per config dimension.

    Returns a dict keyed by dimension name (model, mcps, skills, task_type),
    each containing a list of DimensionStats sorted by mean rating descending.
    """
    if not ratings:
        return {}

    by_dimension: dict[str, dict[str, list[int]]] = defaultdict(
        lambda: defaultdict(list)
    )

    for r in ratings:
        if r.config.model:
            by_dimension["model"][r.config.model].append(r.rating)

        mcp_key = ",".join(r.config.mcps) if r.config.mcps else "(none)"
        by_dimension["mcps"][mcp_key].append(r.rating)

        skill_key = ",".join(r.config.skills) if r.config.skills else "(none)"
        by_dimension["skills"][skill_key].append(r.rating)

        if r.task_type:
            by_dimension["task_type"][r.task_type].append(r.rating)

    result: dict[str, list[DimensionStats]] = {}
    for dim_name, values in sorted(by_dimension.items()):
        stats_list: list[DimensionStats] = []
        for value, scores in sorted(
            values.items(), key=lambda x: -statistics.mean(x[1])
        ):
            stdev = statistics.stdev(scores) if len(scores) > 1 else None
            stats_list.append(
                DimensionStats(
                    dimension=dim_name,
                    value=value,
                    count=len(scores),
                    mean=statistics.mean(scores),
                    stdev=stdev,
                    median=statistics.median(scores),
                )
            )
        result[dim_name] = stats_list

    return result
