"""Analyze codeprobe-2txc — preamble-effect rerun.

Reads:
- runs/with-sg-tuned-preamble/results.json  (NEW — 15 oracle_checks trials with tuned preamble)
  from gascity-oc-rerun-ttwq experiment dir
- runs.codeprobe-ttwq/baseline/results.json  (oc baseline reference, 15 trials)
- runs.codeprobe-ttwq/with-sourcegraph/results.json  (oc with-sg DEFAULT-preamble reference, 15 trials)
- runs.codeprobe-mcn7/baseline/results.json  (SDLC baseline reference, 15 trials)
- runs.codeprobe-mcn7/with-sourcegraph/results.json  (SDLC with-sg TUNED-preamble; mcn7 already had tuning, 15 trials)
- runs.codeprobe-3oms/with-sourcegraph/results.json  (SDLC with-sg DEFAULT-preamble at N=1, 5 trials)

Produces:
- per_trial.json — flat trials with task_id, config, family, repeat, reward, cost, etc.
- per_family_summary.json — per-config / per-task / per-family aggregates + deltas
- aggregate.json — codeprobe-style envelope summary
"""

from __future__ import annotations

import json
import math
import statistics as stats
from pathlib import Path

OC_ROOT = Path("/home/ds/test_repos/gascity/gascity-oc-rerun-ttwq/.codeprobe")
SDLC_ROOT = Path("/home/ds/test_repos/gascity/gascity-mcp-comparison/.codeprobe")
OUT_DIR = Path("/home/ds/projects/codeprobe/docs/investigations/codeprobe-2txc")

OC_TASKS = ["oc_001", "oc_002", "oc_003", "oc_004", "oc_005"]
SDLC_TASKS = ["ba1f3675", "d906ac3d", "0d4ec3ad", "45b581b5", "fde8e6e0"]


def _load_completed(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    data = json.loads(path.read_text())
    return list(data.get("completed", []))


def _flatten(rows: list[dict], config_label: str, family: str) -> list[dict]:
    out = []
    for c in rows:
        sd = c.get("scoring_details") or {}
        out.append(
            {
                "config": config_label,
                "family": family,
                "task_id": c.get("task_id"),
                "repeat_index": c.get("repeat_index", 0),
                "reward": float(c.get("automated_score", 0.0) or 0.0),
                "status": c.get("status"),
                "duration_seconds": c.get("duration_seconds"),
                "cost_usd": c.get("cost_usd"),
                "input_tokens": c.get("input_tokens"),
                "output_tokens": c.get("output_tokens"),
                "cache_read_tokens": c.get("cache_read_tokens"),
                "scorer_family": sd.get("scorer_family"),
                "sub_scores": sd.get("sub_scores"),
                "tool_use_by_name": c.get("tool_use_by_name"),
            }
        )
    return out


def _t_crit(df: int) -> float:
    table = {
        2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306,
        9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
    }
    return table.get(df, 1.96)


def _paired_t(deltas: list[float]) -> dict:
    n = len(deltas)
    if n < 2:
        return {"n": n, "mean": deltas[0] if n else 0.0, "t": None, "ci95_lower": None, "ci95_upper": None}
    m = stats.mean(deltas)
    sd = stats.stdev(deltas)
    if sd == 0.0:
        return {"n": n, "mean": round(m, 4), "std": 0.0, "t": None, "df": n - 1, "ci95_lower": round(m, 4), "ci95_upper": round(m, 4)}
    se = sd / math.sqrt(n)
    t = m / se
    df = n - 1
    tc = _t_crit(df)
    return {
        "n": n,
        "mean": round(m, 4),
        "std": round(sd, 4),
        "t": round(t, 3),
        "df": df,
        "ci95_lower": round(m - tc * se, 4),
        "ci95_upper": round(m + tc * se, 4),
    }


def _per_task_aggregates(trials: list[dict], config: str, tasks: list[str]) -> dict:
    out = {}
    for tid in tasks:
        rs = [t for t in trials if t["config"] == config and t["task_id"] == tid]
        if not rs:
            out[tid] = {"n": 0}
            continue
        rewards = [r["reward"] for r in rs]
        costs = [r["cost_usd"] for r in rs if r.get("cost_usd") is not None]
        wallclocks = [r["duration_seconds"] for r in rs if r.get("duration_seconds") is not None]
        out[tid] = {
            "n": len(rs),
            "mean_reward": round(stats.mean(rewards), 4),
            "std_reward": round(stats.stdev(rewards), 4) if len(rewards) >= 2 else 0.0,
            "rewards": [round(r, 4) for r in rewards],
            "mean_cost_usd": round(stats.mean(costs), 4) if costs else None,
            "mean_wallclock_s": round(stats.mean(wallclocks), 1) if wallclocks else None,
            "wallclocks": [round(w, 1) for w in wallclocks],
        }
    return out


def _config_summary(trials: list[dict], config: str) -> dict:
    rs = [t for t in trials if t["config"] == config]
    if not rs:
        return {"n": 0}
    rewards = [r["reward"] for r in rs]
    costs = [r["cost_usd"] for r in rs if r.get("cost_usd") is not None]
    return {
        "n": len(rs),
        "mean_reward": round(stats.mean(rewards), 4),
        "std_reward": round(stats.stdev(rewards), 4) if len(rewards) >= 2 else 0.0,
        "total_cost_usd": round(sum(costs), 4),
        "mean_cost_per_trial": round(stats.mean(costs), 4) if costs else None,
    }


def _paired_delta(
    trials: list[dict], cfg_a: str, cfg_b: str, tasks: list[str]
) -> dict:
    """Pair the i-th repeat of cfg_a with the i-th repeat of cfg_b for each task.

    Returns paired-t delta = mean(cfg_a - cfg_b) over all paired trials.
    """
    pair_deltas = []
    per_task = {}
    for tid in tasks:
        a = sorted(
            [t for t in trials if t["config"] == cfg_a and t["task_id"] == tid],
            key=lambda r: r["repeat_index"],
        )
        b = sorted(
            [t for t in trials if t["config"] == cfg_b and t["task_id"] == tid],
            key=lambda r: r["repeat_index"],
        )
        n = min(len(a), len(b))
        deltas = [a[i]["reward"] - b[i]["reward"] for i in range(n)]
        for d in deltas:
            pair_deltas.append(d)
        per_task[tid] = {
            "n_pairs": n,
            "deltas": [round(d, 4) for d in deltas],
            "a_mean": round(stats.mean(a[i]["reward"] for i in range(n)), 4) if n else None,
            "b_mean": round(stats.mean(b[i]["reward"] for i in range(n)), 4) if n else None,
            "paired_t": _paired_t(deltas),
        }
    return {
        "cfg_a_minus_cfg_b": f"{cfg_a} - {cfg_b}",
        "per_task": per_task,
        "family_paired_t": _paired_t(pair_deltas),
    }


def _wallclock_compare(trials: list[dict], cfg_a: str, cfg_b: str, tasks: list[str]) -> dict:
    out = {}
    a_means = []
    b_means = []
    for tid in tasks:
        a = [t["duration_seconds"] for t in trials if t["config"] == cfg_a and t["task_id"] == tid and t.get("duration_seconds") is not None]
        b = [t["duration_seconds"] for t in trials if t["config"] == cfg_b and t["task_id"] == tid and t.get("duration_seconds") is not None]
        ma = stats.mean(a) if a else None
        mb = stats.mean(b) if b else None
        if ma is not None:
            a_means.append(ma)
        if mb is not None:
            b_means.append(mb)
        out[tid] = {
            "a_mean_s": round(ma, 1) if ma else None,
            "b_mean_s": round(mb, 1) if mb else None,
            "delta_s": round(ma - mb, 1) if (ma is not None and mb is not None) else None,
            "delta_pct": round(100 * (ma - mb) / mb, 1) if (ma is not None and mb) else None,
        }
    return {
        "cfg_a_minus_cfg_b": f"{cfg_a} - {cfg_b}",
        "per_task": out,
        "family_mean_a": round(stats.mean(a_means), 1) if a_means else None,
        "family_mean_b": round(stats.mean(b_means), 1) if b_means else None,
        "family_delta_pct": (
            round(100 * (stats.mean(a_means) - stats.mean(b_means)) / stats.mean(b_means), 1)
            if (a_means and b_means) else None
        ),
    }


def _criterion_table(trials: list[dict], task_id: str) -> dict:
    """For oracle_checks tasks, gather per-criterion sub_scores across trials."""
    rs = [t for t in trials if t["task_id"] == task_id]
    by_cfg = {}
    for r in rs:
        cfg = r["config"]
        sub = r.get("sub_scores") or {}
        crits = sub.get("criterion_scores") or {}
        if cfg not in by_cfg:
            by_cfg[cfg] = []
        by_cfg[cfg].append(crits)
    return by_cfg


def main() -> None:
    new_oc_tuned = _flatten(
        _load_completed(OC_ROOT / "runs" / "with-sg-tuned-preamble" / "results.json"),
        "oc_with-sg-tuned-preamble",
        "oracle_checks",
    )
    oc_baseline = _flatten(
        _load_completed(OC_ROOT / "runs.codeprobe-ttwq" / "baseline" / "results.json"),
        "oc_baseline",
        "oracle_checks",
    )
    oc_with_sg_default = _flatten(
        _load_completed(OC_ROOT / "runs.codeprobe-ttwq" / "with-sourcegraph" / "results.json"),
        "oc_with-sg-default-preamble",
        "oracle_checks",
    )

    sdlc_baseline = _flatten(
        _load_completed(SDLC_ROOT / "runs.codeprobe-mcn7" / "baseline" / "results.json"),
        "sdlc_baseline",
        "continuous",
    )
    # mcn7 with-sg already had the SDLC-tuned preamble — relabel accordingly
    sdlc_with_sg_tuned = _flatten(
        [c for c in _load_completed(SDLC_ROOT / "runs.codeprobe-mcn7" / "with-sourcegraph" / "results.json")
         if c.get("task_id") in SDLC_TASKS],
        "sdlc_with-sg-tuned-preamble",
        "continuous",
    )
    # 3oms had default preamble for SDLC; only N=1
    sdlc_with_sg_default_3oms = _flatten(
        [c for c in _load_completed(SDLC_ROOT / "runs.codeprobe-3oms" / "with-sourcegraph" / "results.json")
         if c.get("task_id") in SDLC_TASKS],
        "sdlc_with-sg-default-preamble-3oms",
        "continuous",
    )

    all_trials = (
        new_oc_tuned + oc_baseline + oc_with_sg_default
        + sdlc_baseline + sdlc_with_sg_tuned + sdlc_with_sg_default_3oms
    )

    print(f"Trial counts:")
    for cfg in sorted({t['config'] for t in all_trials}):
        n = sum(1 for t in all_trials if t["config"] == cfg)
        print(f"  {cfg}: {n}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "per_trial.json").write_text(json.dumps(all_trials, indent=2))

    summary: dict = {
        "n_trials": len(all_trials),
        "configs": {
            "new (this bead)": ["oc_with-sg-tuned-preamble"],
            "oracle_checks reference (ttwq)": ["oc_baseline", "oc_with-sg-default-preamble"],
            "SDLC reference (mcn7)": ["sdlc_baseline", "sdlc_with-sg-tuned-preamble"],
            "SDLC default-preamble reference (3oms, N=1)": ["sdlc_with-sg-default-preamble-3oms"],
        },
        "config_summaries": {
            cfg: _config_summary(all_trials, cfg)
            for cfg in sorted({t['config'] for t in all_trials})
        },
        # Per-task aggregates
        "per_task_oc": {
            cfg: _per_task_aggregates(all_trials, cfg, OC_TASKS)
            for cfg in ["oc_baseline", "oc_with-sg-default-preamble", "oc_with-sg-tuned-preamble"]
        },
        "per_task_sdlc": {
            cfg: _per_task_aggregates(all_trials, cfg, SDLC_TASKS)
            for cfg in ["sdlc_baseline", "sdlc_with-sg-tuned-preamble", "sdlc_with-sg-default-preamble-3oms"]
        },
        # Contrasts: oracle_checks
        "contrast_oc_tuned_vs_baseline": _paired_delta(
            all_trials, "oc_with-sg-tuned-preamble", "oc_baseline", OC_TASKS
        ),
        "contrast_oc_tuned_vs_default_preamble": _paired_delta(
            all_trials, "oc_with-sg-tuned-preamble", "oc_with-sg-default-preamble", OC_TASKS
        ),
        # Reference: oracle_checks default vs baseline (already computed in ttwq, replicate here)
        "contrast_oc_default_vs_baseline": _paired_delta(
            all_trials, "oc_with-sg-default-preamble", "oc_baseline", OC_TASKS
        ),
        # Contrasts: SDLC
        "contrast_sdlc_tuned_vs_baseline": _paired_delta(
            all_trials, "sdlc_with-sg-tuned-preamble", "sdlc_baseline", SDLC_TASKS
        ),
        # SDLC tuned (N=3) vs SDLC default (N=1, 3oms): use first repeat of tuned for fair pairing
        "contrast_sdlc_tuned_vs_default_preamble_n1": _paired_delta(
            all_trials, "sdlc_with-sg-tuned-preamble", "sdlc_with-sg-default-preamble-3oms", SDLC_TASKS
        ),
        # Wall-clock: tuned vs default (3oms reference)
        "wallclock_sdlc_tuned_vs_default": _wallclock_compare(
            all_trials, "sdlc_with-sg-tuned-preamble", "sdlc_with-sg-default-preamble-3oms", SDLC_TASKS
        ),
        # Per-criterion sub_scores for oc_004 across tuned vs default
        "oc_004_criterion_table": _criterion_table(all_trials, "oc_004"),
    }

    (OUT_DIR / "per_family_summary.json").write_text(json.dumps(summary, indent=2))

    # Mini-aggregate file similar to codeprobe interpret envelope
    aggregate = {
        "experiment": "codeprobe-2txc-preamble-tune-rerun",
        "config_summaries": summary["config_summaries"],
        "key_contrasts": {
            "oc_tuned_vs_baseline": summary["contrast_oc_tuned_vs_baseline"]["family_paired_t"],
            "oc_tuned_vs_default_preamble": summary["contrast_oc_tuned_vs_default_preamble"]["family_paired_t"],
            "oc_default_vs_baseline": summary["contrast_oc_default_vs_baseline"]["family_paired_t"],
            "sdlc_tuned_vs_baseline": summary["contrast_sdlc_tuned_vs_baseline"]["family_paired_t"],
            "sdlc_tuned_vs_default_preamble_n1": summary["contrast_sdlc_tuned_vs_default_preamble_n1"]["family_paired_t"],
        },
    }
    (OUT_DIR / "aggregate.json").write_text(json.dumps(aggregate, indent=2))

    print("\n=== config summaries ===")
    print(json.dumps(summary["config_summaries"], indent=2))
    print("\n=== key contrasts (family-level paired-t) ===")
    print(json.dumps(aggregate["key_contrasts"], indent=2))
    print("\n=== oc_004 criterion table ===")
    print(json.dumps(summary["oc_004_criterion_table"], indent=2))


if __name__ == "__main__":
    main()
