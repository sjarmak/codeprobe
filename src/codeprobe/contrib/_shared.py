"""Shared constants and helpers for contrib modules."""

from __future__ import annotations

from codeprobe.models.experiment import ConfigResults

PASS_THRESHOLD: float = 0.5
"""Score at or above which a task is considered a pass."""


def build_score_maps(
    configs: list[ConfigResults],
) -> dict[str, dict[str, float]]:
    """Build {config_label: {task_id: score}} from config results.

    Args:
        configs: List of config results.

    Returns:
        Nested dict mapping config label to task_id to automated_score.
    """
    return {
        cr.config: {t.task_id: t.automated_score for t in cr.completed}
        for cr in configs
    }
