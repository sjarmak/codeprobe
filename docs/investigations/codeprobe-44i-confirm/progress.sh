#!/usr/bin/env bash
# Quick progress snapshot of the 9tk run (no sqlite3 CLI here — use python).
T=/home/ds/test_repos/gascity/gascity-mcp-comparison/.codeprobe
INV=/home/ds/projects/codeprobe/docs/investigations/codeprobe-44i-confirm

echo "=== proc ==="
pgrep -af "codeprobe run" | grep -vE "/bin/bash -c|pgrep|progress.sh|launch-9tk" || echo "NOT RUNNING"

echo "=== trials per arm (target 30 each; 90 total) ==="
python3 - "$T" <<'PY'
import sqlite3, sys, glob, os, json
T = sys.argv[1]
total = 0
for db in sorted(glob.glob(f"{T}/runs/*/checkpoint.db")):
    lbl = os.path.basename(os.path.dirname(db))
    try:
        c = sqlite3.connect(db)
        rows = c.execute("select status, automated_score, result_json from checkpoints").fetchall()
    except Exception as e:
        print(f"  {lbl}: db error {e}"); continue
    n = len(rows); total += n
    ok = sum(1 for s, _, _ in rows if s == "completed")
    err = sum(1 for s, _, _ in rows if s in ("error", "failed"))
    mean = (sum(a for _, a, _ in rows) / n) if n else 0.0
    capn = clip = 0
    for _, _, rj in rows:
        try:
            d = json.loads(rj) if rj else {}
        except Exception:
            d = {}
        if d.get("result_subtype") == "error_max_turns":
            capn += 1
        ts = d.get("duration_seconds") or 0
        if ts and ts >= 10750:
            clip += 1
    print(f"  {lbl:14s}: {n:2d}/30  (done={ok} err={err} mean={mean:.3f} maxturn_hits={capn} clips={clip})")
print(f"  TOTAL: {total}/90")
PY

echo "=== latest stderr tail ==="
ls -t "$INV"/logs-9tk/*.stderr.log 2>/dev/null | head -1 | xargs tail -5 2>/dev/null

echo "=== budget/halt markers ==="
ls -t "$INV"/logs-9tk/*.stderr.log 2>/dev/null | head -1 | xargs grep -iE "budget exceeded|quota|session limit|halting" 2>/dev/null | tail -4 || true
