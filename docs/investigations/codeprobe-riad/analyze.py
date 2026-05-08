"""Analyze codeprobe-riad — oc_004 rerun under refined oracle_checks preamble + refreshed Sourcegraph index.

Reads:
- runs/with-sg-tuned-preamble/oc_004/{,repeat-1,repeat-2}/scoring.json
  (NEW — 3 trials with refined preamble + fresh SG index)
- runs.codeprobe-2txc/with-sg-tuned-preamble/oc_004/{,repeat-1,repeat-2}/scoring.json
  (REFERENCE — 3 trials with codeprobe-ovz2 tuned preamble + STALE SG index)
- runs.codeprobe-ttwq/with-sourcegraph/oc_004/{,repeat-1,repeat-2}/scoring.json
  (REFERENCE — 3 trials with default preamble + STALE SG index)
- runs.codeprobe-ttwq/baseline/oc_004/{,repeat-1,repeat-2}/scoring.json
  (CEILING — 3 trials no MCP, baseline; toml_tag = 3/3 here)

Produces:
- per_trial.json — flat trials with task_id, config, repeat, reward, criterion_scores, diagnostics.
- aggregate.json — per-config means and per-criterion breakdown matching the writeup table.
"""

from __future__ import annotations

import json
import statistics as stats
from pathlib import Path

OC_ROOT = Path("/home/ds/test_repos/gascity/gascity-oc-rerun-ttwq/.codeprobe")
OUT_DIR = Path("/home/ds/projects/codeprobe/docs/investigations/codeprobe-riad")

CONFIGS = [
    ("riad-refined-preamble-fresh-index", OC_ROOT / "runs/with-sg-tuned-preamble/oc_004"),
    ("2txc-tuned-preamble-stale-index", OC_ROOT / "runs.codeprobe-2txc/with-sg-tuned-preamble/oc_004"),
    ("ttwq-default-preamble-stale-index", OC_ROOT / "runs.codeprobe-ttwq/with-sourcegraph/oc_004"),
    ("ttwq-baseline-no-mcp", OC_ROOT / "runs.codeprobe-ttwq/baseline/oc_004"),
]


def _load_repeats(task_dir: Path) -> list[dict]:
    """Return list of scoring.json dicts for repeat 0..N (repeat 0 sits in task_dir root)."""
    out: list[dict] = []
    root_score = task_dir / "scoring.json"
    if root_score.is_file():
        out.append(json.loads(root_score.read_text()))
    for i in range(1, 10):
        rep_score = task_dir / f"repeat-{i}" / "scoring.json"
        if rep_score.is_file():
            out.append(json.loads(rep_score.read_text()))
    return out


def _trial_row(config: str, repeat: int, scoring: dict) -> dict:
    diag = scoring.get("diagnostics") or {}
    return {
        "config": config,
        "task_id": "oc_004",
        "repeat_index": repeat,
        "reward": float(scoring.get("reward", 0.0)),
        "passed": bool(scoring.get("passed", False)),
        "criterion_scores": scoring.get("criterion_scores", {}),
        "scorer_family": scoring.get("scorer_family"),
        "duration_seconds": diag.get("task_time_seconds"),
        "cost_usd": diag.get("token_cost_usd"),
        "input_tokens": diag.get("input_tokens"),
        "output_tokens": diag.get("output_tokens"),
        "cache_read_tokens": diag.get("cache_read_tokens"),
        "cache_creation_tokens": diag.get("cache_creation_tokens"),
    }


def _config_summary(rows: list[dict]) -> dict:
    rewards = [r["reward"] for r in rows]
    costs = [r["cost_usd"] for r in rows if r.get("cost_usd") is not None]
    walls = [r["duration_seconds"] for r in rows if r.get("duration_seconds") is not None]
    out_tokens = [r["output_tokens"] for r in rows if r.get("output_tokens") is not None]

    crit_keys = sorted({k for r in rows for k in (r.get("criterion_scores") or {})})
    per_crit: dict[str, list[float]] = {k: [] for k in crit_keys}
    for r in rows:
        for k, v in (r.get("criterion_scores") or {}).items():
            per_crit[k].append(float(v))

    summary = {
        "n": len(rows),
        "mean_reward": round(stats.mean(rewards), 4) if rewards else 0.0,
        "rewards": [round(r, 4) for r in rewards],
        "mean_cost_usd": round(stats.mean(costs), 4) if costs else None,
        "mean_wallclock_s": round(stats.mean(walls), 1) if walls else None,
        "wallclocks": [round(w, 1) for w in walls],
        "mean_output_tokens": round(stats.mean(out_tokens), 0) if out_tokens else None,
        "criterion_means": {k: round(stats.mean(v), 3) for k, v in per_crit.items()},
        "criterion_per_repeat": {k: v for k, v in per_crit.items()},
    }
    return summary


def main() -> None:
    per_trial: list[dict] = []
    per_config: dict[str, dict] = {}

    for config_label, task_dir in CONFIGS:
        repeats = _load_repeats(task_dir)
        rows = [_trial_row(config_label, i, s) for i, s in enumerate(repeats)]
        per_trial.extend(rows)
        per_config[config_label] = _config_summary(rows) if rows else {"n": 0}

    (OUT_DIR / "per_trial.json").write_text(json.dumps(per_trial, indent=2))

    aggregate = {
        "task": "oc_004",
        "rubric_criteria": ["names_flag_aliases_field", "names_toml_tag", "explains_schema_driven_rationale", "names_resolve_path"],
        "per_config": per_config,
        "key_finding": (
            "Refined preamble (verify-before-denying) + freshly-reindexed Sourcegraph "
            "lifts oc_004 from mean reward 0.595 (2txc) to 1.000 (3/3 perfect). "
            "All four criteria — including the previously-broken names_toml_tag — "
            "now score 1.0 across all three repeats. Cost holds at $0.31/trial."
        ),
        "attribution_caveat": (
            "Two variables changed simultaneously between the 2txc reference and this run: "
            "(1) preamble.py gained the verify-via-Grep-before-denying instruction; "
            "(2) Sourcegraph's mirror was updated, lifting the indexed commit from "
            "99742e36 (2026-04-22, before the FlagAliases commit d906ac3d on 2026-04-27) "
            "to 6b5d9121 (origin/main HEAD, which contains FlagAliases). "
            "We cannot disentangle preamble effect from index-refresh effect with this run alone."
        ),
    }
    (OUT_DIR / "aggregate.json").write_text(json.dumps(aggregate, indent=2))

    print(json.dumps(aggregate, indent=2))


if __name__ == "__main__":
    main()
