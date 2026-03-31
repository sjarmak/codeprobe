"""Elo rating system for experiment configurations.

Treats each shared task as a "match" between two configs.
Higher score wins. Updates ratings using standard Elo formula.
"""

from __future__ import annotations

from codeprobe.models.experiment import ConfigResults

_DEFAULT_RATING = 1500.0
_K_FACTOR = 32.0


def compute_elo_ratings(
    configs: list[ConfigResults],
    initial_rating: float = _DEFAULT_RATING,
    k: float = _K_FACTOR,
) -> dict[str, float]:
    """Compute Elo ratings from pairwise task comparisons.

    Args:
        configs: List of config results with shared task IDs.
        initial_rating: Starting Elo for all configs.
        k: K-factor controlling rating sensitivity.

    Returns:
        Mapping of config label to final Elo rating.
    """
    ratings: dict[str, float] = {cr.config: initial_rating for cr in configs}

    score_maps: dict[str, dict[str, float]] = {}
    for cr in configs:
        score_maps[cr.config] = {t.task_id: t.automated_score for t in cr.completed}

    labels = list(score_maps.keys())
    for i, a in enumerate(labels):
        for b in labels[i + 1 :]:
            shared = sorted(set(score_maps[a].keys()) & set(score_maps[b].keys()))
            for tid in shared:
                sa, sb = score_maps[a][tid], score_maps[b][tid]
                if sa > sb:
                    actual_a, actual_b = 1.0, 0.0
                elif sb > sa:
                    actual_a, actual_b = 0.0, 1.0
                else:
                    actual_a, actual_b = 0.5, 0.5

                expected_a = 1.0 / (1.0 + 10.0 ** ((ratings[b] - ratings[a]) / 400.0))
                expected_b = 1.0 - expected_a

                ratings[a] += k * (actual_a - expected_a)
                ratings[b] += k * (actual_b - expected_b)

    return ratings
