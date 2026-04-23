"""Stability test — running the gate twice on the same holdout must produce
stable scores within +/- 0.1 (in practice: exactly identical, since the
computation is pure arithmetic over a fixed input).
"""

from __future__ import annotations

import math

import pytest

from codeprobe.calibration import (
    HoldoutRow,
    compute_pearson,
    emit_profile,
)


def _fixed_holdout() -> tuple[HoldoutRow, ...]:
    """A hand-picked 100-row, 3-repo holdout with a clear positive signal.

    Deterministic: no RNG, no clock. Running the test on any machine must
    give the same correlation value to floating-point precision.
    """
    rows: list[HoldoutRow] = []
    # 3 repos x ~34 tasks = 102 rows (>= 100 min_tasks, >= 3 min_repos).
    for repo_idx in range(3):
        repo_name = f"repo_{repo_idx}"
        for task_idx in range(34):
            # Curator A walks [0.02, 0.68] in steps of 0.02.
            a = (task_idx + 1) * 0.02
            # Curator B agrees strongly but not perfectly: small fixed
            # jitter derived from task index (no RNG).
            jitter = ((task_idx % 5) - 2) * 0.01  # in [-0.02, 0.02]
            b = a + jitter
            rows.append(
                HoldoutRow(
                    task_id=f"{repo_name}__task_{task_idx}",
                    curator_a=a,
                    curator_b=b,
                    repo=repo_name,
                )
            )
    return tuple(rows)


class TestStability:
    def test_pearson_identical_across_calls(self) -> None:
        rows = _fixed_holdout()
        xs = [r.curator_a for r in rows]
        ys = [r.curator_b for r in rows]

        first = compute_pearson(xs, ys)
        second = compute_pearson(xs, ys)

        # Pure arithmetic — must be bit-identical.
        assert first == second
        # And well within the ±0.1 acceptance window.
        assert abs(first - second) <= 0.1

    def test_emit_profile_correlation_stable(self) -> None:
        rows = _fixed_holdout()
        first = emit_profile(rows, curator_version="stable-v1")
        second = emit_profile(rows, curator_version="stable-v1")

        assert math.isclose(
            first.correlation_coefficient,
            second.correlation_coefficient,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
        assert (
            abs(first.correlation_coefficient - second.correlation_coefficient)
            <= 0.1
        )
        assert first.holdout_size == second.holdout_size
        assert first.holdout_repos == second.holdout_repos

    @pytest.mark.parametrize("run_count", [3, 5])
    def test_many_runs_all_agree(self, run_count: int) -> None:
        rows = _fixed_holdout()
        results = [
            emit_profile(rows, curator_version="stable-v1").correlation_coefficient
            for _ in range(run_count)
        ]
        # All results within ±0.1 of the mean (well within — they're
        # identical here, but the acceptance criterion uses ±0.1).
        mean = sum(results) / len(results)
        for r in results:
            assert abs(r - mean) <= 0.1
