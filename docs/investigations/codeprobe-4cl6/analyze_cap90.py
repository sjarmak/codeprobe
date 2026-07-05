"""codeprobe-4cl6.2 analyzer — cap=90 sweep point.

Reads with-sg-cap90 scoring.json files (5 SDLC tasks at N=3) plus the
claude CLI result line from each trial's agent_output.txt (num_turns,
subtype), then joins against:

- with-sg-cap75 trials loaded directly from the same runs dir (sibling
  sweep point codeprobe-4cl6.1) — primary pair-test the bead asks for,
- aupz `with-sg-fixed` (cap=50) per_trial.json — second pair-test,
- mcn7 `baseline` and `with-sourcegraph` (uncapped) per_trial.json —
  parent A3 reference (does cap=90 reward CI overlap mcn7 baseline?).

Output filenames are cap90-suffixed so they never collide with the
cap75 analyzer's outputs in this shared investigation directory.

Pure deterministic arithmetic — no semantic judgment. ZFC-compliant.
Adapted from docs/investigations/codeprobe-4cl6/analyze.py (cap75).
"""

from __future__ import annotations

import datetime
import json
import math
import statistics
from pathlib import Path

RUNS_DIR = Path(
    "/home/ds/test_repos/gascity/gascity-mcp-comparison/.codeprobe/runs"
)
OUT_DIR = Path("/home/ds/projects/codeprobe/docs/investigations/codeprobe-4cl6")
AUPZ_DIR = Path("/home/ds/projects/codeprobe/docs/investigations/codeprobe-aupz")
MCN7_DIR = Path("/home/ds/projects/codeprobe/docs/investigations/codeprobe-mcn7")

NEW_CONFIG = "with-sg-cap90"
SIBLING_CONFIG = "with-sg-cap75"
SDLC_TASKS = ("ba1f3675", "d906ac3d", "0d4ec3ad", "45b581b5", "fde8e6e0")
N_REPEATS = 3

# Provenance guard (mechanical timestamp filter, not semantic judgment):
# the first cap75 run (2026-06-12 11:28) died in a session-limit cascade
# and its artifacts share the runs dir with the 13:49 rerun. Any
# scoring.json older than the rerun start is dead-run residue and must be
# treated as missing, not silently mixed into the pair-test.
CAP75_RERUN_EPOCH = datetime.datetime(2026, 6, 12, 13, 49, 0).timestamp()


def _trial_dir(config: str, task: str, repeat: int) -> Path:
    base = RUNS_DIR / config / task
    return base if repeat == 0 else base / f"repeat-{repeat}"


def _read_result_line(trial_dir: Path) -> dict:
    """Parse the terminal claude CLI result record from agent_output.txt.

    Returns {} when the file or a parseable result line is absent (e.g.
    trial still running or agent crashed before emitting a result).
    """
    p = trial_dir / "agent_output.txt"
    if not p.exists():
        return {}
    lines = [ln for ln in p.read_text().splitlines() if ln.strip()]
    for ln in reversed(lines):
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if rec.get("type") == "result":
            return rec
    return {}


def load_config_trials(config: str) -> list[dict]:
    trials: list[dict] = []
    for task in SDLC_TASKS:
        for repeat in range(N_REPEATS):
            d = _trial_dir(config, task, repeat)
            p = d / "scoring.json"
            stale = (
                config == SIBLING_CONFIG
                and p.exists()
                and p.stat().st_mtime < CAP75_RERUN_EPOCH
            )
            if not p.exists() or stale:
                trials.append(
                    {
                        "config": config,
                        "task_id": task,
                        "repeat_index": repeat,
                        "missing": True,
                        "stale_dead_run_artifact": stale,
                    }
                )
                continue
            data = json.loads(p.read_text())
            diag = data.get("diagnostics", {}) or {}
            result = _read_result_line(d)
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
                    "sub_scores": data.get("sub_scores"),
                    "task_time_seconds": diag.get("task_time_seconds"),
                    "token_cost_usd": diag.get("token_cost_usd"),
                    "input_tokens": diag.get("input_tokens"),
                    "output_tokens": diag.get("output_tokens"),
                    "cache_read_tokens": diag.get("cache_read_tokens"),
                    "cache_creation_tokens": diag.get("cache_creation_tokens"),
                    "num_turns": result.get("num_turns"),
                    "result_subtype": result.get("subtype"),
                    "hit_max_turns": result.get("subtype") == "error_max_turns",
                    "missing": False,
                }
            )
    return trials


def load_baselines() -> dict:
    mcn7 = json.loads((MCN7_DIR / "per_trial.json").read_text())
    aupz = json.loads((AUPZ_DIR / "per_trial.json").read_text())
    return {
        "mcn7-baseline": [t for t in mcn7 if t["config"] == "baseline"],
        "mcn7-with-sg": [t for t in mcn7 if t["config"] == "with-sourcegraph"],
        "aupz-with-sg-fixed": [
            t
            for t in aupz
            if t["config"] == "with-sg-fixed" and t["task_id"] in SDLC_TASKS
        ],
    }


def _safe_sum(values: list[float | None]) -> float:
    return sum(v for v in values if v is not None)


def _safe_mean(values: list[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    return statistics.mean(clean) if clean else None


def per_task_aggregate(trials: list[dict], tasks: tuple[str, ...]) -> dict:
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
            "mean_time_s": _safe_mean([t.get("task_time_seconds") for t in sel]),
            "total_time_s": _safe_sum([t.get("task_time_seconds") for t in sel]),
            "total_cost_usd": _safe_sum([t.get("token_cost_usd") for t in sel]),
            "total_input_tokens": _safe_sum([t.get("input_tokens") for t in sel]),
            "total_output_tokens": _safe_sum(
                [t.get("output_tokens") for t in sel]
            ),
            "total_cache_read_tokens": _safe_sum(
                [t.get("cache_read_tokens") for t in sel]
            ),
            "num_turns": [t.get("num_turns") for t in sel],
            "cap_hits": sum(1 for t in sel if t.get("hit_max_turns")),
        }
    return out


def family_aggregate(per_task: dict, tasks: tuple[str, ...]) -> dict:
    task_means = [
        per_task[t]["mean_reward"]
        for t in tasks
        if per_task[t]["mean_reward"] is not None
    ]
    n_trials = sum(per_task[t]["n"] for t in tasks)
    return {
        "n_trials": n_trials,
        "mean_reward": statistics.mean(task_means) if task_means else None,
        "total_time_s": sum(per_task[t]["total_time_s"] for t in tasks),
        "mean_time_s_per_trial": (
            sum(per_task[t]["total_time_s"] for t in tasks) / max(n_trials, 1)
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
        "cap_hits": sum(per_task[t]["cap_hits"] for t in tasks),
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
    t_crit_table = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
                    6: 2.447, 7: 2.365, 14: 2.145}
    t_crit = t_crit_table.get(df, 2.776)
    return {
        "n": n,
        "deltas": deltas,
        "mean": mean,
        "std": sd,
        "se": se,
        "df": df,
        "t_crit_95": t_crit,
        "ci_95": [mean - t_crit * se, mean + t_crit * se],
        "t_stat": mean / se if se > 0 else float("inf"),
    }


def render_per_task_table(
    label_a: str,
    label_b: str,
    a_per_task: dict,
    b_per_task: dict,
    tasks: tuple[str, ...],
) -> str:
    rows = [
        f"| task | metric | {label_a} | {label_b} | delta |",
        "|------|--------|" + "-" * (len(label_a) + 2) + "|"
        + "-" * (len(label_b) + 2) + "|-------|",
    ]
    for t in tasks:
        a, b = a_per_task[t], b_per_task[t]

        def _fmt_pair(field: str, fmt: str = "{:.3f}") -> tuple[str, str, str]:
            av, bv = a.get(field), b.get(field)
            if av is None or bv is None:
                return "—", "—", "—"
            return fmt.format(av), fmt.format(bv), fmt.format(av - bv)

        rew = _fmt_pair("mean_reward", "{:.4f}")
        tim = _fmt_pair("mean_time_s", "{:.0f}s")
        cost = _fmt_pair("total_cost_usd", "${:.2f}")
        outt = _fmt_pair("total_output_tokens", "{:.0f}")
        rows += [
            f"| {t} | reward (mean) | {rew[0]} | {rew[1]} | {rew[2]} |",
            f"| {t} | wall-clock (mean/trial) | {tim[0]} | {tim[1]} | {tim[2]} |",
            f"| {t} | cost (total over 3) | {cost[0]} | {cost[1]} | {cost[2]} |",
            f"| {t} | output tokens (total) | {outt[0]} | {outt[1]} | {outt[2]} |",
        ]
    return "\n".join(rows)


def render_cap_hit_table(trials: list[dict]) -> str:
    rows = [
        "| task | repeat 0 | repeat 1 | repeat 2 |",
        "|------|----------|----------|----------|",
    ]
    for task in SDLC_TASKS:
        cells = []
        for repeat in range(N_REPEATS):
            t = next(
                (
                    x
                    for x in trials
                    if x["task_id"] == task and x["repeat_index"] == repeat
                ),
                None,
            )
            if t is None or t.get("missing"):
                cells.append("missing")
                continue
            tag = "hit" if t.get("hit_max_turns") else "finished"
            turns = t.get("num_turns")
            reward = t.get("reward")
            cells.append(
                f"{tag} (turns={turns}, r={reward:.3f})"
                if reward is not None
                else f"{tag} (turns={turns})"
            )
        rows.append(f"| {task} | {cells[0]} | {cells[1]} | {cells[2]} |")
    return "\n".join(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trials = load_config_trials(NEW_CONFIG)
    (OUT_DIR / "per_trial_cap90.json").write_text(
        json.dumps(trials, indent=2) + "\n"
    )

    n_missing = sum(1 for t in trials if t.get("missing"))
    if n_missing:
        print(f"WARNING: {n_missing}/15 cap90 trials missing — run incomplete")

    cap75_trials = load_config_trials(SIBLING_CONFIG)
    n75_missing = sum(1 for t in cap75_trials if t.get("missing"))
    if n75_missing:
        print(
            f"WARNING: {n75_missing}/15 cap75 sibling trials missing — "
            "cap75 contrast computed on the completed subset"
        )

    baselines = load_baselines()
    cap90 = per_task_aggregate(trials, SDLC_TASKS)
    cap75 = per_task_aggregate(cap75_trials, SDLC_TASKS)
    aupz = per_task_aggregate(baselines["aupz-with-sg-fixed"], SDLC_TASKS)
    mcn7_base = per_task_aggregate(baselines["mcn7-baseline"], SDLC_TASKS)
    mcn7_sg = per_task_aggregate(baselines["mcn7-with-sg"], SDLC_TASKS)

    contrasts = {
        "cap90_vs_cap75": paired_delta(cap90, cap75, SDLC_TASKS),
        "cap90_vs_aupz-cap50": paired_delta(cap90, aupz, SDLC_TASKS),
        "cap90_vs_mcn7-baseline": paired_delta(cap90, mcn7_base, SDLC_TASKS),
        "cap90_vs_mcn7-with-sg": paired_delta(cap90, mcn7_sg, SDLC_TASKS),
    }
    family = {
        "with-sg-cap90": family_aggregate(cap90, SDLC_TASKS),
        "with-sg-cap75": family_aggregate(cap75, SDLC_TASKS),
        "aupz-with-sg-fixed-cap50": family_aggregate(aupz, SDLC_TASKS),
        "mcn7-baseline": family_aggregate(mcn7_base, SDLC_TASKS),
        "mcn7-with-sourcegraph": family_aggregate(mcn7_sg, SDLC_TASKS),
    }
    (OUT_DIR / "per_family_summary_cap90.json").write_text(
        json.dumps({"family": family, "contrasts": contrasts}, indent=2) + "\n"
    )

    sections = ["# cap90 analysis tables\n"]
    sections.append("## Family rollup\n")
    sections.append(json.dumps(family, indent=1))
    for name, c in contrasts.items():
        sections.append(f"\n## Paired contrast: {name}\n")
        sections.append(json.dumps(c, indent=1))
    sections.append("\n## Per-task: cap90 vs cap75\n")
    sections.append(
        render_per_task_table("cap90", "cap75", cap90, cap75, SDLC_TASKS)
    )
    sections.append("\n## Per-task: cap90 vs aupz cap50\n")
    sections.append(
        render_per_task_table("cap90", "cap50(aupz)", cap90, aupz, SDLC_TASKS)
    )
    sections.append("\n## Per-task: cap90 vs mcn7 baseline\n")
    sections.append(
        render_per_task_table(
            "cap90", "baseline(mcn7)", cap90, mcn7_base, SDLC_TASKS
        )
    )
    sections.append("\n## Cap-hit map (cap90)\n")
    sections.append(render_cap_hit_table(trials))
    hits = family["with-sg-cap90"]["cap_hits"]
    n = family["with-sg-cap90"]["n_trials"]
    sections.append(
        f"\nmax-turns hit rate: {hits}/{n}"
        f" = {100.0 * hits / n:.1f}%" if n else "\nno trials"
    )
    (OUT_DIR / "analyze_cap90.out").write_text("\n".join(sections) + "\n")
    print("\n".join(sections))


if __name__ == "__main__":
    main()
