#!/usr/bin/env bash
# codeprobe-4cl6.3 — launch the with-sg-uncapped control sweep.
#
# Run ONLY when no sibling `codeprobe run` is alive (cap75/cap90 share the
# target dir, .codeprobe/experiment.json, and the (codeprobe-4cl6, run)
# tenant lock; the lock is acquired before load_experiment, so swapping
# experiment.json under a live run poisons it).
#
# --max-cost-usd 90: mcn7's uncapped with-sourcegraph arm cost $85.17 for
# the same 15 trials; the soft cap must not halt the run before 15/15 or
# acceptance (complete-trials) fails. 90 covers the mcn7-level worst case
# while keeping a hard stop.
set -euo pipefail

TARGET=/home/ds/test_repos/gascity/gascity-mcp-comparison
INVDIR=/home/ds/projects/codeprobe/docs/investigations/codeprobe-4cl6
SUITE=/home/ds/projects/codeprobe/docs/investigations/codeprobe-aupz/suite-sdlc.toml
LOGDIR="$INVDIR/logs-sdlc-uncapped"

if pgrep -f "codeprobe run" > /dev/null; then
    echo "ABORT: a codeprobe run process is still alive:" >&2
    pgrep -af "codeprobe run" >&2
    exit 1
fi

# Back up only when the current config isn't already ours — a budget-halt
# relaunch (checkpoint resume) must not clobber the pre-uncapped backup.
if ! grep -q '"with-sg-uncapped"' "$TARGET/.codeprobe/experiment.json"; then
    cp "$TARGET/.codeprobe/experiment.json" \
       "$TARGET/.codeprobe/experiment.json.bak.pre-uncapped"
fi
cp "$INVDIR/experiment-uncapped.json" "$TARGET/.codeprobe/experiment.json"
mkdir -p "$LOGDIR"

# Unique log names per invocation so a resume relaunch doesn't truncate
# the previous attempt's logs.
STAMP=$(date +%Y%m%d-%H%M%S)
STDOUT_LOG="$LOGDIR/run-$STAMP.stdout.log"
STDERR_LOG="$LOGDIR/run-$STAMP.stderr.log"

cd "$TARGET"
# setsid: own process session — survives launcher-session killpg (run1 of the
# cap90 lane died to exactly this; nohup alone does not protect against it).
setsid nohup codeprobe run . \
    --suite "$SUITE" \
    --repeats 3 \
    --parallel 2 \
    --max-cost-usd 90 \
    --tenant codeprobe-4cl6 \
    > "$STDOUT_LOG" 2> "$STDERR_LOG" &
PID=$!
echo "launched pid $PID (logs: $STDOUT_LOG)"

# Verify the run loaded the uncapped config before declaring success.
sleep 20
if ! kill -0 "$PID" 2>/dev/null; then
    echo "ABORT: run process died within 20s; stderr tail:" >&2
    tail -20 "$STDERR_LOG" >&2
    exit 1
fi
if grep -q "with-sg-uncapped" "$STDOUT_LOG" "$STDERR_LOG"; then
    echo "confirmed: run is executing config with-sg-uncapped (pid $PID)"
else
    echo "ABORT: launched run does not show config with-sg-uncapped — killing pid $PID" >&2
    kill "$PID"
    exit 1
fi
