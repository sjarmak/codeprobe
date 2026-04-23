"""Datadog intake exporter.

Produces a single JSON artefact conforming to the Datadog events/metrics
intake envelope:

```
{
  "series": [
    {"metric": "<name>", "points": [[<ts>, <value>]], "tags": ["config:..", "task_id:.."]},
    ...
  ],
  "events": [
    {"title": "<title>", "text": "<body>", "tags": ["config:..", "task_id:.."]},
    ...
  ]
}
```

The transform is mechanical: every numeric field on an entry becomes a
metric series point, and every entry produces exactly one event summarising
whether the task passed. No LLM, no network — callers POST the artefact to
Datadog themselves.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from codeprobe.snapshot.exporters._common import load_entries, load_manifest

__all__ = ["export_datadog"]

# Keys whose presence is structural (identifiers / labels) rather than
# metric values — these never become series points.
_LABEL_KEYS: frozenset[str] = frozenset({"config", "task_id", "task", "id"})


def _resolve_timestamp(manifest: dict[str, Any]) -> int:
    """Return an epoch-seconds timestamp sourced from ``created_at``.

    Falls back to ``int(time.time())`` when the manifest is missing or has
    an unparseable ``created_at`` — the series is still valid either way.
    """
    created_at = manifest.get("created_at") if isinstance(manifest, dict) else None
    if isinstance(created_at, str):
        try:
            # Python accepts ISO-8601 via fromisoformat (with 3.11+ Z support).
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            return int(dt.timestamp())
        except ValueError:
            pass
    return int(time.time())


def _entry_label_tags(entry: dict[str, Any]) -> list[str]:
    """Return ``key:value`` label tags for an entry (config + task id)."""
    tags: list[str] = []
    for key in ("config", "task_id", "task", "id"):
        value = entry.get(key)
        if value is None or value == "":
            continue
        tags.append(f"{key}:{value}")
    return tags


def _numeric_value(value: Any) -> float | None:
    """Return ``value`` as float when numeric (incl. bool), else None."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _build_series(
    entries: list[dict[str, Any]], timestamp: int
) -> list[dict[str, Any]]:
    series: list[dict[str, Any]] = []
    for entry in entries:
        tags = _entry_label_tags(entry)
        for key in sorted(entry.keys()):
            if key in _LABEL_KEYS:
                continue
            numeric = _numeric_value(entry[key])
            if numeric is None:
                continue
            series.append(
                {
                    "metric": f"codeprobe.{key}",
                    "points": [[timestamp, numeric]],
                    "tags": list(tags),
                }
            )
    return series


def _build_events(
    entries: list[dict[str, Any]], timestamp: int
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for entry in entries:
        tags = _entry_label_tags(entry)
        task_label = entry.get("task_id") or entry.get("task") or entry.get("id") or "<unknown>"
        passed = entry.get("passed")
        outcome = (
            "passed"
            if passed is True
            else "failed"
            if passed is False
            else "unknown"
        )
        events.append(
            {
                "title": f"codeprobe task {task_label}",
                "text": f"Outcome: {outcome}",
                "tags": list(tags),
                "date_happened": timestamp,
            }
        )
    return events


def export_datadog(snapshot_dir: Path, out_path: Path) -> Path:
    """Write a Datadog intake artefact for ``snapshot_dir`` to ``out_path``.

    Returns the resolved ``out_path`` after writing. The file is a
    deterministic JSON document (sorted keys, 2-space indent) so consecutive
    runs against identical inputs produce byte-identical output.
    """
    snapshot_dir = Path(snapshot_dir)
    out_path = Path(out_path)

    manifest = load_manifest(snapshot_dir)
    entries = load_entries(snapshot_dir)
    timestamp = _resolve_timestamp(manifest)

    payload: dict[str, Any] = {
        "series": _build_series(entries, timestamp),
        "events": _build_events(entries, timestamp),
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return out_path
