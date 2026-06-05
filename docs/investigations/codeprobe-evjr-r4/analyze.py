#!/usr/bin/env python3
"""codeprobe-evjr.4 three-arm analysis.

Reads per-config results.json + the shared trace.db produced by the
gascity-mcp-comparison run and emits the metrics the bead's acceptance
criteria require:

  * per-config tool histogram (built-in vs mcp__sourcegraph__*)
  * read-traffic split (local Read vs mcp__sourcegraph__read_file)
  * output_tokens mean/sum  -> narrow vs with-sourcegraph delta
  * cost / time / reward     -> narrow vs baseline / with-sourcegraph

Pure arithmetic aggregation over recorded fields (ZFC-clean: no semantic
judgement, no thresholds baked into control flow).
"""
import json
import sqlite3
import statistics
from pathlib import Path

RUNS = Path("/home/ds/test_repos/gascity/gascity-mcp-comparison/.codeprobe/runs")
CONFIGS = ["baseline", "with-sourcegraph", "with-sg-narrow"]


def load_trials(config: str) -> list[dict]:
    path = RUNS / config / "results.json"
    data = json.loads(path.read_text())
    return data.get("completed", [])


def mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else 0.0


def summarize(config: str) -> dict:
    trials = load_trials(config)
    tool_hist: dict[str, int] = {}
    for t in trials:
        for name, n in (t.get("tool_use_by_name") or {}).items():
            tool_hist[name] = tool_hist.get(name, 0) + n
    out = {
        "config": config,
        "n": len(trials),
        "reward_mean": mean([t.get("automated_score") for t in trials]),
        "cost_total": sum(t.get("cost_usd") or 0.0 for t in trials),
        "cost_mean": mean([t.get("cost_usd") for t in trials]),
        "time_mean": mean([t.get("duration_seconds") for t in trials]),
        "out_tok_mean": mean([t.get("output_tokens") for t in trials]),
        "out_tok_sum": sum(t.get("output_tokens") or 0 for t in trials),
        "tool_hist": dict(sorted(tool_hist.items(), key=lambda kv: -kv[1])),
    }
    # read-traffic split
    local_read = tool_hist.get("Read", 0)
    mcp_read = tool_hist.get("mcp__sourcegraph__read_file", 0)
    total_read = local_read + mcp_read
    out["local_read"] = local_read
    out["mcp_read_file"] = mcp_read
    out["read_total"] = total_read
    out["local_read_share"] = (local_read / total_read) if total_read else None
    return out


def main():
    rows = [summarize(c) for c in CONFIGS]
    by = {r["config"]: r for r in rows}

    print("=" * 88)
    print(f"{'config':<18}{'n':>3}{'reward':>9}{'cost$':>9}{'cost/t':>8}"
          f"{'time_s':>9}{'out_tok':>10}{'localRead%':>11}")
    print("-" * 88)
    for r in rows:
        share = "" if r["local_read_share"] is None else f"{r['local_read_share']*100:.0f}%"
        print(f"{r['config']:<18}{r['n']:>3}{r['reward_mean']:>9.3f}"
              f"{r['cost_total']:>9.2f}{r['cost_mean']:>8.2f}"
              f"{r['time_mean']:>9.0f}{r['out_tok_mean']:>10.0f}{share:>11}")
    print("=" * 88)

    base, sg, narrow = by["baseline"], by["with-sourcegraph"], by["with-sg-narrow"]

    def ratio(a, b):
        return (a / b) if b else float("inf")

    print("\n# Acceptance-criteria checks")
    # 1. output_tokens drop narrow vs with-sourcegraph
    drop = 1 - ratio(narrow["out_tok_mean"], sg["out_tok_mean"])
    print(f"1. output_tokens narrow vs with-sourcegraph: "
          f"{narrow['out_tok_mean']:.0f} vs {sg['out_tok_mean']:.0f}  "
          f"=> {drop*100:+.1f}%  (predicted >= -30%): "
          f"{'PASS' if drop >= 0.30 else 'FAIL'}")
    # 2. local Read share >= 30% of read traffic in narrow
    lrs = narrow["local_read_share"]
    print(f"2. narrow local Read share of read traffic: "
          f"{'n/a' if lrs is None else f'{lrs*100:.0f}%'} "
          f"(local={narrow['local_read']}, mcp_read={narrow['mcp_read_file']}) "
          f"(>= 30%): {'PASS' if (lrs or 0) >= 0.30 else 'FAIL'}")
    # 3. reward holds within +/-0.05 of baseline
    dr = narrow["reward_mean"] - base["reward_mean"]
    holds = abs(dr) <= 0.05
    print(f"3. reward narrow vs baseline: {narrow['reward_mean']:.3f} vs "
          f"{base['reward_mean']:.3f} => {dr:+.3f}  "
          f"(within +/-0.05: {'YES' if holds else 'NO'}; "
          f"direction: {'better' if dr > 0 else 'worse' if dr < 0 else 'equal'})")
    # cost deltas
    print(f"\n# Cost / time context")
    print(f"   cost narrow vs with-sourcegraph: ${narrow['cost_total']:.2f} vs "
          f"${sg['cost_total']:.2f} ({(ratio(narrow['cost_total'], sg['cost_total'])-1)*100:+.1f}%)")
    print(f"   cost narrow vs baseline:         ${narrow['cost_total']:.2f} vs "
          f"${base['cost_total']:.2f} ({(ratio(narrow['cost_total'], base['cost_total'])-1)*100:+.1f}%)")

    print("\n# Per-config tool histograms")
    for r in rows:
        print(f"\n[{r['config']}]  (n={r['n']})")
        for name, n in r["tool_hist"].items():
            print(f"    {n:6d}  {name}")

    Path(__file__).with_name("per_config_summary.json").write_text(
        json.dumps(rows, indent=2)
    )
    print("\nwrote per_config_summary.json")


if __name__ == "__main__":
    main()
