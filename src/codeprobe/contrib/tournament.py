"""Round-robin tournament ranking for experiment configurations.

Pits every pair of configs against each other on shared tasks.
A config "wins" a task if its score is strictly higher.
"""

from __future__ import annotations

from dataclasses import dataclass

from codeprobe.models.experiment import ConfigResults


@dataclass(frozen=True)
class Standing:
    """Tournament standing for one configuration."""

    label: str
    wins: int
    losses: int
    draws: int


def round_robin(configs: list[ConfigResults]) -> list[Standing]:
    """Run a round-robin tournament across configurations.

    Compares each pair on their shared tasks. Returns standings
    sorted by wins descending, then losses ascending.
    """
    score_maps: dict[str, dict[str, float]] = {}
    for cr in configs:
        score_maps[cr.config] = {t.task_id: t.automated_score for t in cr.completed}

    records: dict[str, list[int]] = {cr.config: [0, 0, 0] for cr in configs}  # W, L, D

    labels = list(score_maps.keys())
    for i, a in enumerate(labels):
        for b in labels[i + 1 :]:
            shared = set(score_maps[a].keys()) & set(score_maps[b].keys())
            for tid in shared:
                sa, sb = score_maps[a][tid], score_maps[b][tid]
                if sa > sb:
                    records[a][0] += 1
                    records[b][1] += 1
                elif sb > sa:
                    records[b][0] += 1
                    records[a][1] += 1
                else:
                    records[a][2] += 1
                    records[b][2] += 1

    standings = [
        Standing(label=label, wins=r[0], losses=r[1], draws=r[2])
        for label, r in records.items()
    ]
    return sorted(standings, key=lambda s: (-s.wins, s.losses))
