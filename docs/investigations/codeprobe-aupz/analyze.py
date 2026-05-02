"""codeprobe-aupz analyzer.

Reads with-sg-fixed scoring.json files (5 SDLC + 5 oracle_checks at N=3),
joins against mcn7 + ttwq per_trial.json baselines, and emits comparison
tables for the four contrasts the bead asks for.

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
OUT_DIR = Path("/home/ds/projects/codeprobe/docs/investigations/codeprobe-aupz")
MCN7_DIR = Path("/home/ds/projects/codeprobe/docs/investigations/codeprobe-mcn7")
TTWQ_DIR = Path("/home/ds/projects/codeprobe/docs/investigations/codeprobe-ttwq")

NEW_CONFIG = "with-sg-fixed"
SDLC_TASKS = ("ba1f3675", "d906ac3d", "0d4ec3ad", "45b581b5", "fde8e6e0")
OC_TASKS = ("oc_001", "oc_002", "oc_003", "oc_004", "oc_005")
N_REPEATS = 3


def _scoring_path(config: str, task: str, repeat: int) -> Path:
    base = RUNS_DIR / config / task
    return (
        base / "scoring.json"
        if repeat == 0
        else base / f"repeat-{repeat}" / "scoring.json"
    )


def load_with_sg_fixed_trials(tasks: tuple[str, ...]) -> list[dict]:
    """Load with-sg-fixed scoring.json into the same per_trial schema as mcn7."""
    trials: list[dict] = []
    for task in tasks:
        for repeat in range(N_REPEATS):
            p = _scoring_path(NEW_CONFIG, task, repeat)
            if not p.exists():
                trials.append(
                    {
                        "config": NEW_CONFIG,
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
                    "config": NEW_CONFIG,
                    "task_id": task,
                    "repeat_index": repeat,
                    "reward": data.get("reward", data.get("score")),
                    "score": data.get("score"),
                    "status": data.get("status"),
                    "scorer_family": data.get("scorer_family"),
                    "passed": data.get("passed"),
                    "sub_scores": data.get("sub_scores"),
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


def _normalize(trial: dict) -> dict:
    """Normalize ttwq's duration/cost field names onto the mcn7 schema."""
    t = dict(trial)
    if "task_time_seconds" not in t and "duration_seconds" in t:
        t["task_time_seconds"] = t.get("duration_seconds")
    if "token_cost_usd" not in t and "cost_usd" in t:
        t["token_cost_usd"] = t.get("cost_usd")
    return t


def load_baselines() -> dict:
    """Return {family: {config: [trial, ...]}} for the four reuse buckets."""
    mcn7 = json.loads((MCN7_DIR / "per_trial.json").read_text())
    ttwq = json.loads((TTWQ_DIR / "per_trial.json").read_text())
    return {
        "sdlc": {
            "baseline": [_normalize(t) for t in mcn7 if t["config"] == "baseline"],
            "with-sourcegraph": [
                _normalize(t) for t in mcn7 if t["config"] == "with-sourcegraph"
            ],
        },
        "oracle_checks": {
            "baseline": [
                _normalize(t) for t in ttwq if t["config"] == "baseline"
            ],
            "with-sourcegraph": [
                _normalize(t) for t in ttwq if t["config"] == "with-sourcegraph"
            ],
        },
    }


def _safe_sum(values: list[float | None]) -> float:
    return sum(v for v in values if v is not None)


def _safe_mean(values: list[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    return statistics.mean(clean) if clean else None


def _safe_std(values: list[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    return statistics.stdev(clean) if len(clean) > 1 else 0.0 if clean else None


def per_task_aggregate(trials: list[dict], tasks: tuple[str, ...]) -> dict:
    """Per-task aggregate across repeats for a single config's trials."""
    out: dict = {}
    for task in tasks:
        sel = [
            t for t in trials if t["task_id"] == task and not t.get("missing")
        ]
        rewards = [t["reward"] for t in sel if t.get("reward") is not None]
        out[task] = {
            "n": len(sel),
            "rewards": rewards,
            "mean_reward": _safe_mean(rewards),
            "std_reward": _safe_std(rewards),
            "mean_time_s": _safe_mean(
                [t.get("task_time_seconds") for t in sel]
            ),
            "total_time_s": _safe_sum(
                [t.get("task_time_seconds") for t in sel]
            ),
            "mean_cost_usd": _safe_mean(
                [t.get("token_cost_usd") for t in sel]
            ),
            "total_cost_usd": _safe_sum(
                [t.get("token_cost_usd") for t in sel]
            ),
            "total_input_tokens": _safe_sum(
                [t.get("input_tokens") for t in sel]
            ),
            "total_output_tokens": _safe_sum(
                [t.get("output_tokens") for t in sel]
            ),
            "total_cache_read_tokens": _safe_sum(
                [t.get("cache_read_tokens") for t in sel]
            ),
        }
    return out


def family_aggregate(per_task: dict, tasks: tuple[str, ...]) -> dict:
    """Family-level totals + means across tasks (each task contributes equal
    weight to the family-level mean reward)."""
    task_means = [
        per_task[t]["mean_reward"]
        for t in tasks
        if per_task[t]["mean_reward"] is not None
    ]
    return {
        "n_trials": sum(per_task[t]["n"] for t in tasks),
        "mean_reward": statistics.mean(task_means) if task_means else None,
        "total_time_s": sum(per_task[t]["total_time_s"] for t in tasks),
        "mean_time_s_per_trial": (
            sum(per_task[t]["total_time_s"] for t in tasks)
            / max(sum(per_task[t]["n"] for t in tasks), 1)
        ),
        "total_cost_usd": sum(per_task[t]["total_cost_usd"] for t in tasks),
        "total_input_tokens": sum(
            per_task[t]["total_input_tokens"] for t in tasks
        ),
        "total_output_tokens": sum(
            per_task[t]["total_output_tokens"] for t in tasks
        ),
        "total_cache_read_tokens": sum(
            per_task[t]["total_cache_read_tokens"] for t in tasks
        ),
    }


def paired_delta(a_per_task: dict, b_per_task: dict, tasks: tuple[str, ...]):
    """Paired (a - b) per-task delta with 95% CI via t (df=n-1)."""
    deltas = []
    for t in tasks:
        a = a_per_task[t]["mean_reward"]
        b = b_per_task[t]["mean_reward"]
        if a is None or b is None:
            continue
        deltas.append(a - b)
    n = len(deltas)
    if n == 0:
        return {"n": 0, "deltas": []}
    if n == 1:
        return {"n": 1, "deltas": deltas, "mean": deltas[0]}
    mean = statistics.mean(deltas)
    sd = statistics.stdev(deltas)
    se = sd / math.sqrt(n)
    df = n - 1
    # paired-t critical value for 95% two-sided at df=4 ≈ 2.776
    t_crit_table = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
                    6: 2.447, 7: 2.365, 14: 2.145}
    t_crit = t_crit_table.get(df, 2.776)
    ci_low = mean - t_crit * se
    ci_high = mean + t_crit * se
    t_stat = mean / se if se > 0 else float("inf")
    return {
        "n": n,
        "deltas": deltas,
        "mean": mean,
        "std": sd,
        "se": se,
        "df": df,
        "t_crit_95": t_crit,
        "ci_95": [ci_low, ci_high],
        "t_stat": t_stat,
    }


def render_per_task_table(
    label_a: str,
    label_b: str,
    a_per_task: dict,
    b_per_task: dict,
    tasks: tuple[str, ...],
) -> str:
    """Render a per-task time/tokens/cost/reward comparison table."""
    rows = ["| task | metric | " + label_a + " | " + label_b + " | delta |",
            "|------|--------|" + "-" * (len(label_a) + 2) + "|" + "-" * (len(label_b) + 2) + "|-------|"]
    for t in tasks:
        a = a_per_task[t]
        b = b_per_task[t]

        def _fmt_pair(field: str, fmt: str = "{:.3f}") -> tuple[str, str, str]:
            av = a.get(field)
            bv = b.get(field)
            if av is None or bv is None:
                return "—", "—", "—"
            return (
                fmt.format(av),
                fmt.format(bv),
                fmt.format(av - bv),
            )

        rew_a, rew_b, rew_d = _fmt_pair("mean_reward", "{:.4f}")
        time_a, time_b, time_d = _fmt_pair("mean_time_s", "{:.0f}s")
        cost_a, cost_b, cost_d = _fmt_pair("total_cost_usd", "${:.2f}")
        in_a, in_b, in_d = _fmt_pair("total_input_tokens", "{:.0f}")
        out_a, out_b, out_d = _fmt_pair("total_output_tokens", "{:.0f}")
        cr_a, cr_b, cr_d = _fmt_pair("total_cache_read_tokens", "{:.0f}")

        rows += [
            f"| {t} | reward (mean) | {rew_a} | {rew_b} | {rew_d} |",
            f"| {t} | wall-clock (mean/trial) | {time_a} | {time_b} | {time_d} |",
            f"| {t} | cost (total over 3) | {cost_a} | {cost_b} | {cost_d} |",
            f"| {t} | input tokens (total) | {in_a} | {in_b} | {in_d} |",
            f"| {t} | output tokens (total) | {out_a} | {out_b} | {out_d} |",
            f"| {t} | cache_read tokens (total) | {cr_a} | {cr_b} | {cr_d} |",
        ]
    return "\n".join(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sdlc_new = load_with_sg_fixed_trials(SDLC_TASKS)
    oc_new = load_with_sg_fixed_trials(OC_TASKS)
    new_trials = sdlc_new + oc_new
    (OUT_DIR / "per_trial.json").write_text(
        json.dumps(new_trials, indent=2) + "\n"
    )

    baselines = load_baselines()

    sdlc_new_per_task = per_task_aggregate(sdlc_new, SDLC_TASKS)
    sdlc_baseline_per_task = per_task_aggregate(
        baselines["sdlc"]["baseline"], SDLC_TASKS
    )
    sdlc_with_sg_per_task = per_task_aggregate(
        baselines["sdlc"]["with-sourcegraph"], SDLC_TASKS
    )

    oc_new_per_task = per_task_aggregate(oc_new, OC_TASKS)
    oc_baseline_per_task = per_task_aggregate(
        baselines["oracle_checks"]["baseline"], OC_TASKS
    )
    oc_with_sg_per_task = per_task_aggregate(
        baselines["oracle_checks"]["with-sourcegraph"], OC_TASKS
    )

    contrasts = {
        "sdlc": {
            "with-sg-fixed_vs_with-sg": paired_delta(
                sdlc_new_per_task, sdlc_with_sg_per_task, SDLC_TASKS
            ),
            "with-sg-fixed_vs_baseline": paired_delta(
                sdlc_new_per_task, sdlc_baseline_per_task, SDLC_TASKS
            ),
        },
        "oracle_checks": {
            "with-sg-fixed_vs_with-sg": paired_delta(
                oc_new_per_task, oc_with_sg_per_task, OC_TASKS
            ),
            "with-sg-fixed_vs_baseline": paired_delta(
                oc_new_per_task, oc_baseline_per_task, OC_TASKS
            ),
        },
    }

    family = {
        "sdlc": {
            "baseline": family_aggregate(sdlc_baseline_per_task, SDLC_TASKS),
            "with-sourcegraph": family_aggregate(
                sdlc_with_sg_per_task, SDLC_TASKS
            ),
            "with-sg-fixed": family_aggregate(sdlc_new_per_task, SDLC_TASKS),
        },
        "oracle_checks": {
            "baseline": family_aggregate(oc_baseline_per_task, OC_TASKS),
            "with-sourcegraph": family_aggregate(
                oc_with_sg_per_task, OC_TASKS
            ),
            "with-sg-fixed": family_aggregate(oc_new_per_task, OC_TASKS),
        },
    }

    summary = {
        "per_task": {
            "sdlc": {
                "baseline": sdlc_baseline_per_task,
                "with-sourcegraph": sdlc_with_sg_per_task,
                "with-sg-fixed": sdlc_new_per_task,
            },
            "oracle_checks": {
                "baseline": oc_baseline_per_task,
                "with-sourcegraph": oc_with_sg_per_task,
                "with-sg-fixed": oc_new_per_task,
            },
        },
        "family": family,
        "contrasts": contrasts,
    }
    (OUT_DIR / "per_family_summary.json").write_text(
        json.dumps(summary, indent=2, default=str) + "\n"
    )

    # Print the four primary contrasts as markdown tables for easy copy
    # into the writeup.
    print("=" * 78)
    print("CONTRAST 1 — SDLC: with-sg-fixed vs mcn7 with-sourcegraph")
    print("=" * 78)
    print(
        render_per_task_table(
            "with-sg-fixed",
            "with-sg(mcn7)",
            sdlc_new_per_task,
            sdlc_with_sg_per_task,
            SDLC_TASKS,
        )
    )
    print()
    print("=" * 78)
    print("CONTRAST 2 — SDLC: with-sg-fixed vs mcn7 baseline")
    print("=" * 78)
    print(
        render_per_task_table(
            "with-sg-fixed",
            "baseline(mcn7)",
            sdlc_new_per_task,
            sdlc_baseline_per_task,
            SDLC_TASKS,
        )
    )
    print()
    print("=" * 78)
    print("CONTRAST 3 — oracle_checks: with-sg-fixed vs ttwq with-sourcegraph")
    print("=" * 78)
    print(
        render_per_task_table(
            "with-sg-fixed",
            "with-sg(ttwq)",
            oc_new_per_task,
            oc_with_sg_per_task,
            OC_TASKS,
        )
    )
    print()
    print("=" * 78)
    print("CONTRAST 4 — oracle_checks: with-sg-fixed vs ttwq baseline")
    print("=" * 78)
    print(
        render_per_task_table(
            "with-sg-fixed",
            "baseline(ttwq)",
            oc_new_per_task,
            oc_baseline_per_task,
            OC_TASKS,
        )
    )

    print()
    print("=" * 78)
    print("FAMILY ROLLUP")
    print("=" * 78)
    print(json.dumps(family, indent=2, default=str))
    print()
    print("=" * 78)
    print("PAIRED CONTRASTS (95% CI on per-task delta)")
    print("=" * 78)
    print(json.dumps(contrasts, indent=2, default=str))


if __name__ == "__main__":
    main()
