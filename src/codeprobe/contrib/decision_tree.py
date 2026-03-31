"""Decision tree analysis — find which task features predict config wins.

Groups tasks by a metadata feature and shows which config performs
best in each group, revealing where different setups excel.
"""

from __future__ import annotations

from codeprobe.models.experiment import ConfigResults

_PASS_THRESHOLD = 0.5


def build_decision_tree(
    configs: list[ConfigResults],
    feature_key: str = "difficulty",
) -> dict[str, dict[str, float]]:
    """Build a simple feature-based decision tree.

    Groups tasks by the value of ``feature_key`` in their metadata,
    then computes pass rate per config per group.

    Args:
        configs: List of config results (must share task IDs).
        feature_key: Metadata key to split on.

    Returns:
        Nested dict: {feature_value: {config_label: pass_rate}}.
    """
    groups: dict[str, dict[str, list[float]]] = {}

    for cr in configs:
        for task in cr.completed:
            feature_val = task.metadata.get(feature_key, "unknown")
            if feature_val not in groups:
                groups[feature_val] = {}
            if cr.config not in groups[feature_val]:
                groups[feature_val][cr.config] = []
            groups[feature_val][cr.config].append(task.automated_score)

    result: dict[str, dict[str, float]] = {}
    for feature_val, config_scores in groups.items():
        result[feature_val] = {
            label: (
                sum(1.0 for s in scores if s >= _PASS_THRESHOLD) / len(scores)
                if scores
                else 0.0
            )
            for label, scores in config_scores.items()
        }

    return result
