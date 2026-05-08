"""Analysis script for codeprobe-mcn7: SDLC family rerun at N=3.

Reads per-config results.json + per-(task,repeat) scoring.json, computes:
  - per-trial flat table → per_trial.json
  - per-(config, task) mean reward + std + cost across N=3 repeats
  - paired per-task delta (with-sg − baseline) and 95% CI / paired-t on the 5-task family
  - 0d4ec3ad-specific reproducibility check
  - writes per_family_summary.json + summary.json

Pure deterministic arithmetic — no semantic judgment. ZFC-compliant.
"""

from __future__ import annotations

import json
import math
import statistics
from pathlib import Path

RUNS_DIR = Path(
    "/home/ds/test_repos/gascity/gascity-mcp-comparison/.codeprobe/runs"
)
OUT_DIR = Path("/home/ds/projects/codeprobe/docs/investigations/codeprobe-mcn7")

CONFIGS = ("baseline", "with-sourcegraph")
TASK_IDS = ("ba1f3675", "d906ac3d", "0d4ec3ad", "45b581b5", "fde8e6e0")
N_REPEATS = 3


def _scoring_path(config: str, task: str, repeat: int) -> Path:
    base = RUNS_DIR / config / task
    return base / "scoring.json" if repeat == 0 else base / f"repeat-{repeat}" / "scoring.json"


def load_per_trial() -> list[dict]:
    trials: list[dict] = []
    for config in CONFIGS:
        for task in TASK_IDS:
            for repeat in range(N_REPEATS):
                p = _scoring_path(config, task, repeat)
                if not p.exists():
                    trials.append(
                        {
                            "config": config,
                            "task_id": task,
                            "repeat_index": repeat,
                            "missing": True,
                        }
                    )
                    continue
                data = json.loads(p.read_text())
                diag = data.get("diagnostics", {}) or {}
                trials.append(
                    {
                        "config": config,
                        "task_id": task,
                        "repeat_index": repeat,
                        "reward": data.get("reward", data.get("score")),
                        "score": data.get("score"),
                        "status": data.get("status"),
                        "scorer_family": data.get("scorer_family"),
                        "passed": data.get("passed"),
                        "task_time_seconds": diag.get("task_time_seconds"),
                        "token_cost_usd": diag.get("token_cost_usd"),
                        "input_tokens": diag.get("input_tokens"),
                        "output_tokens": diag.get("output_tokens"),
                        "cache_read_tokens": diag.get("cache_read_tokens"),
                        "cache_creation_tokens": diag.get("cache_creation_tokens"),
                        "missing": False,
                    }
                )
    return trials


def _stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean": None, "std": None, "min": None, "max": None}
    if len(values) == 1:
        return {"n": 1, "mean": values[0], "std": 0.0, "min": values[0], "max": values[0]}
    return {
        "n": len(values),
        "mean": statistics.mean(values),
        "std": statistics.stdev(values),
        "min": min(values),
        "max": max(values),
    }


def per_task_aggregate(trials: list[dict]) -> dict:
    """Per (config, task) aggregate across repeats."""
    out: dict = {}
    for config in CONFIGS:
        out[config] = {}
        for task in TASK_IDS:
            sel = [
                t
                for t in trials
                if t["config"] == config and t["task_id"] == task and not t.get("missing")
            ]
            rewards = [t["reward"] for t in sel if t["reward"] is not None]
            costs = [t["token_cost_usd"] for t in sel if t.get("token_cost_usd") is not None]
            times = [t["task_time_seconds"] for t in sel if t.get("task_time_seconds") is not None]
            out[config][task] = {
                "n_trials": len(sel),
                "n_completed": sum(1 for t in sel if t.get("status") == "completed"),
                "reward": _stats(rewards),
                "cost_usd": _stats(costs),
                "time_seconds": _stats(times),
                "rewards_per_repeat": rewards,
            }
    return out


def per_task_paired_delta(per_task: dict) -> list[dict]:
    """Pair by task_id: delta = with_sg.mean - baseline.mean."""
    out = []
    for task in TASK_IDS:
        b = per_task["baseline"][task]
        w = per_task["with-sourcegraph"][task]
        b_mean = b["reward"]["mean"]
        w_mean = w["reward"]["mean"]
        delta = (w_mean - b_mean) if (b_mean is not None and w_mean is not None) else None
        out.append(
            {
                "task_id": task,
                "baseline_mean": b_mean,
                "baseline_std": b["reward"]["std"],
                "baseline_n": b["reward"]["n"],
                "with_sg_mean": w_mean,
                "with_sg_std": w["reward"]["std"],
                "with_sg_n": w["reward"]["n"],
                "delta_mean": delta,
                "baseline_rewards": b["rewards_per_repeat"],
                "with_sg_rewards": w["rewards_per_repeat"],
                "baseline_cost_mean": b["cost_usd"]["mean"],
                "with_sg_cost_mean": w["cost_usd"]["mean"],
            }
        )
    return out


def family_delta_with_ci(per_task_deltas: list[dict]) -> dict:
    """5-task family-level paired-t test on per-task delta_mean."""
    deltas = [d["delta_mean"] for d in per_task_deltas if d["delta_mean"] is not None]
    n = len(deltas)
    if n < 2:
        return {
            "n_tasks": n,
            "mean_delta": deltas[0] if n == 1 else None,
            "stderr": None,
            "ci_95_low": None,
            "ci_95_high": None,
            "t_statistic": None,
            "p_value_two_sided": None,
            "method": "insufficient_n",
        }
    mean = statistics.mean(deltas)
    stdev = statistics.stdev(deltas)
    stderr = stdev / math.sqrt(n)
    df = n - 1
    # two-tailed t critical for 95% CI at df=4 ≈ 2.776
    t_crit_95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447}.get(df, 1.96)
    t_stat = mean / stderr if stderr > 0 else float("inf") if mean != 0 else 0.0
    # crude two-sided p-value via normal approximation when df is small (descriptive only)
    z = abs(t_stat)
    p_norm = 2 * (1 - 0.5 * (1 + math.erf(z / math.sqrt(2))))
    return {
        "n_tasks": n,
        "mean_delta": mean,
        "stdev_of_per_task_deltas": stdev,
        "stderr": stderr,
        "ci_95_low": mean - t_crit_95 * stderr,
        "ci_95_high": mean + t_crit_95 * stderr,
        "t_statistic": t_stat,
        "df": df,
        "p_value_normal_approx": p_norm,
        "t_critical_95_two_sided": t_crit_95,
        "method": "paired_t_test_per_task_means_with_t_critical",
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    trials = load_per_trial()
    (OUT_DIR / "per_trial.json").write_text(json.dumps(trials, indent=2) + "\n")

    per_task = per_task_aggregate(trials)
    per_task_deltas = per_task_paired_delta(per_task)
    family = family_delta_with_ci(per_task_deltas)

    summary = {
        "investigation_id": "codeprobe-mcn7",
        "predecessor": "codeprobe-3oms",
        "n_repeats": N_REPEATS,
        "n_tasks": len(TASK_IDS),
        "n_trials_expected": N_REPEATS * len(TASK_IDS) * len(CONFIGS),
        "n_trials_observed": sum(1 for t in trials if not t.get("missing")),
        "n_trials_missing": sum(1 for t in trials if t.get("missing")),
        "per_task": per_task,
        "per_task_deltas": per_task_deltas,
        "family_delta_paired_t": family,
        "task_0d4ec3ad_reproducibility": next(
            (d for d in per_task_deltas if d["task_id"] == "0d4ec3ad"), None
        ),
    }

    (OUT_DIR / "per_family_summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    print(f"Trials observed: {summary['n_trials_observed']}/{summary['n_trials_expected']}")
    print(f"Family mean delta (with-sg − baseline): {family['mean_delta']:+.4f}")
    print(f"  95% CI: [{family['ci_95_low']:+.4f}, {family['ci_95_high']:+.4f}]")
    print(f"  t={family['t_statistic']:+.3f}, df={family['df']}")
    print()
    print("Per-task:")
    for d in per_task_deltas:
        b = d["baseline_rewards"]
        w = d["with_sg_rewards"]
        b_str = "[" + ",".join(f"{x:.3f}" for x in b) + "]" if b else "[]"
        w_str = "[" + ",".join(f"{x:.3f}" for x in w) + "]" if w else "[]"
        print(
            f"  {d['task_id']}: b={d['baseline_mean']!r:>20} {b_str} | "
            f"w={d['with_sg_mean']!r:>20} {w_str} | Δ={d['delta_mean']!r}"
        )


if __name__ == "__main__":
    main()
