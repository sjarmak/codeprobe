"""Analyze codeprobe-ttwq oracle_checks N=3 rerun.

Reads runs/baseline/results.json and runs/with-sourcegraph/results.json
from the gascity-oc-rerun-ttwq experiment and produces:

- per_trial.json — flat list of all trials with task_id, config, repeat, reward, cost
- per_family_summary.json — per-config per-task aggregates
- aggregate.json — mirror of codeprobe report (config_summaries + pairwise_deltas)
- per_task_delta.json — per-task delta with paired-t test
"""

from __future__ import annotations

import json
import math
import statistics as stats
from pathlib import Path

EXP_ROOT = Path("/home/ds/test_repos/gascity/gascity-oc-rerun-ttwq/.codeprobe")
OUT_DIR = Path("/home/ds/projects/codeprobe/docs/investigations/codeprobe-ttwq")

CONFIGS = ["baseline", "with-sourcegraph"]
TASKS = ["oc_001", "oc_002", "oc_003", "oc_004", "oc_005"]


def _load_completed(config: str) -> list[dict]:
    p = EXP_ROOT / "runs" / config / "results.json"
    data = json.loads(p.read_text())
    return list(data.get("completed", []))


def _flat_trials() -> list[dict]:
    rows: list[dict] = []
    for cfg in CONFIGS:
        for c in _load_completed(cfg):
            rows.append(
                {
                    "config": cfg,
                    "task_id": c.get("task_id"),
                    "repeat_index": c.get("repeat_index", 0),
                    "reward": float(c.get("automated_score", 0.0) or 0.0),
                    "status": c.get("status"),
                    "duration_seconds": c.get("duration_seconds"),
                    "cost_usd": c.get("cost_usd"),
                    "input_tokens": c.get("input_tokens"),
                    "output_tokens": c.get("output_tokens"),
                    "cache_read_tokens": c.get("cache_read_tokens"),
                    "scorer_family": (c.get("scoring_details") or {}).get(
                        "scorer_family"
                    ),
                    "sub_scores": (c.get("scoring_details") or {}).get("sub_scores"),
                }
            )
    return rows


def _paired_t(deltas: list[float]) -> dict:
    """One-sample t-test on deltas: H0 mu=0."""
    n = len(deltas)
    if n < 2:
        return {"n": n, "mean": deltas[0] if n else 0.0, "t": None, "p_two_sided": None}
    m = stats.mean(deltas)
    sd = stats.stdev(deltas)
    if sd == 0.0:
        # All deltas identical: trivial outcome
        return {
            "n": n,
            "mean": m,
            "std": 0.0,
            "t": None,
            "p_two_sided": 0.0 if m != 0 else 1.0,
            "ci95_lower": m,
            "ci95_upper": m,
        }
    se = sd / math.sqrt(n)
    t = m / se
    # 95% CI using t_{n-1, 0.975}; df=n-1. Crude approx: t_crit ~= 2.0 for small n
    # Use a small lookup for df=2..10 then 1.96 for larger
    t_crit_lookup = {
        2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306,
        9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
    }
    df = n - 1
    t_crit = t_crit_lookup.get(df, 1.96)
    return {
        "n": n,
        "mean": round(m, 4),
        "std": round(sd, 4),
        "t": round(t, 3),
        "df": df,
        "ci95_lower": round(m - t_crit * se, 4),
        "ci95_upper": round(m + t_crit * se, 4),
    }


def _per_task(trials: list[dict]) -> dict:
    out: dict = {}
    for cfg in CONFIGS:
        for tid in TASKS:
            rs = [t for t in trials if t["config"] == cfg and t["task_id"] == tid]
            rewards = [r["reward"] for r in rs]
            costs = [r["cost_usd"] for r in rs if r.get("cost_usd") is not None]
            out[f"{cfg}|{tid}"] = {
                "n": len(rs),
                "mean_reward": round(stats.mean(rewards), 4) if rewards else None,
                "std_reward": round(stats.stdev(rewards), 4) if len(rewards) >= 2 else 0.0,
                "rewards": [round(r, 4) for r in rewards],
                "mean_cost_usd": round(stats.mean(costs), 4) if costs else None,
                "total_cost_usd": round(sum(costs), 4) if costs else None,
            }
    return out


def _per_task_deltas(trials: list[dict]) -> dict:
    """Paired-t test per task: pair the i-th repeat across configs."""
    out: dict = {}
    for tid in TASKS:
        b = sorted(
            [t for t in trials if t["config"] == "baseline" and t["task_id"] == tid],
            key=lambda r: r["repeat_index"],
        )
        w = sorted(
            [t for t in trials if t["config"] == "with-sourcegraph" and t["task_id"] == tid],
            key=lambda r: r["repeat_index"],
        )
        n_pairs = min(len(b), len(w))
        deltas = [w[i]["reward"] - b[i]["reward"] for i in range(n_pairs)]
        out[tid] = {
            "n_pairs": n_pairs,
            "deltas": [round(d, 4) for d in deltas],
            "baseline_mean": round(stats.mean(b[i]["reward"] for i in range(n_pairs)), 4)
            if n_pairs
            else None,
            "with_sg_mean": round(stats.mean(w[i]["reward"] for i in range(n_pairs)), 4)
            if n_pairs
            else None,
            "paired_t": _paired_t(deltas),
        }
    return out


def _family_delta(trials: list[dict]) -> dict:
    """Family-level paired-t on all 15 pairs (5 tasks × 3 repeats)."""
    pairs: list[float] = []
    for tid in TASKS:
        b = sorted(
            [t for t in trials if t["config"] == "baseline" and t["task_id"] == tid],
            key=lambda r: r["repeat_index"],
        )
        w = sorted(
            [t for t in trials if t["config"] == "with-sourcegraph" and t["task_id"] == tid],
            key=lambda r: r["repeat_index"],
        )
        n_pairs = min(len(b), len(w))
        for i in range(n_pairs):
            pairs.append(w[i]["reward"] - b[i]["reward"])

    bm = stats.mean(
        t["reward"] for t in trials if t["config"] == "baseline"
    )
    wm = stats.mean(
        t["reward"] for t in trials if t["config"] == "with-sourcegraph"
    )
    return {
        "n_pairs": len(pairs),
        "baseline_mean_reward": round(bm, 4),
        "with_sg_mean_reward": round(wm, 4),
        "delta": round(wm - bm, 4),
        "paired_t": _paired_t(pairs),
    }


def _config_summary(trials: list[dict]) -> dict:
    out: dict = {}
    for cfg in CONFIGS:
        rs = [t for t in trials if t["config"] == cfg]
        rewards = [r["reward"] for r in rs]
        costs = [r["cost_usd"] for r in rs if r.get("cost_usd") is not None]
        in_tok = [r["input_tokens"] for r in rs if r.get("input_tokens") is not None]
        out_tok = [r["output_tokens"] for r in rs if r.get("output_tokens") is not None]
        cr_tok = [r["cache_read_tokens"] for r in rs if r.get("cache_read_tokens") is not None]
        out[cfg] = {
            "n": len(rs),
            "mean_reward": round(stats.mean(rewards), 4) if rewards else 0,
            "std_reward": round(stats.stdev(rewards), 4) if len(rewards) >= 2 else 0,
            "total_cost_usd": round(sum(costs), 4),
            "mean_cost_per_task": round(stats.mean(costs), 4) if costs else 0,
            "total_input_tokens": sum(in_tok),
            "total_output_tokens": sum(out_tok),
            "total_cache_read_tokens": sum(cr_tok),
            "score_per_dollar": round(stats.mean(rewards) / (sum(costs) / len(rewards)), 3)
            if costs and rewards
            else None,
        }
    return out


def main() -> None:
    trials = _flat_trials()
    print(f"Total trials: {len(trials)}")

    by_cfg: dict[str, int] = {}
    for t in trials:
        by_cfg[t["config"]] = by_cfg.get(t["config"], 0) + 1
    print(f"Trial counts: {by_cfg}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "per_trial.json").write_text(json.dumps(trials, indent=2))

    summary = {
        "n_trials": len(trials),
        "configs": CONFIGS,
        "tasks": TASKS,
        "config_summaries": _config_summary(trials),
        "per_task": _per_task(trials),
        "per_task_deltas": _per_task_deltas(trials),
        "family_delta": _family_delta(trials),
    }
    (OUT_DIR / "per_family_summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary["config_summaries"], indent=2))
    print("\nFamily delta:")
    print(json.dumps(summary["family_delta"], indent=2))


if __name__ == "__main__":
    main()
