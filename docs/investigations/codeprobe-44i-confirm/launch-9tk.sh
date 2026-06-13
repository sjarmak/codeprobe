#!/usr/bin/env bash
# codeprobe-9tk [44i-confirm] — launch the 3-arm uncapped SDLC confirm run.
#
# 3 arms (local-only / with-sg-narrow / with-sg-full) x 5 SDLC tasks x N=6 = 90
# trials, all uncapped (max_turns=null) with the STEP-0 wall-clock guard
# (extra.timeout_seconds=10800, commit d559f5e). --max-cost-usd 500: the bead's
# $120 predates the uncapped-cost discovery; Stephanie approved N=6 at the
# revised ceiling (projection ~$465). --parallel 2 matches the 4cl6.3 uncapped
# precedent (proven stable for long uncapped trials; higher concurrency raised
# the OAuth-limit-freeze risk seen in the cap-retune).
#
# Clean-start guard: the bare runs/ dir holds a stale with-sg-narrow checkpoint
# from evjr.4 (N=3, cap=50). codeprobe restores checkpoints by config label
# unconditionally, so that dir is archived aside before launch — otherwise the
# narrow arm would resume capped N=3 data and skip trials.
set -euo pipefail

TARGET=/home/ds/test_repos/gascity/gascity-mcp-comparison
INVDIR=/home/ds/projects/codeprobe/docs/investigations/codeprobe-44i-confirm
SUITE=/home/ds/projects/codeprobe/docs/investigations/codeprobe-aupz/suite-sdlc.toml
LOGDIR="$INVDIR/logs-9tk"
STAMP=$(date +%Y%m%d-%H%M%S)

# 1. No sibling run may be alive (shared target dir + experiment.json + lock).
if pgrep -af "codeprobe run" | grep -v "launch-9tk.sh" | grep -q "codeprobe run"; then
    echo "ABORT: a codeprobe run process is still alive:" >&2
    pgrep -af "codeprobe run" | grep -v "launch-9tk.sh" >&2
    exit 1
fi

# 2. Back up the current target config unless it is already ours.
if ! grep -q '"codeprobe-9tk"' "$TARGET/.codeprobe/experiment.json" 2>/dev/null; then
    cp "$TARGET/.codeprobe/experiment.json" \
       "$TARGET/.codeprobe/experiment.json.bak.pre-9tk-$STAMP"
fi

# 3. Archive the bare runs/ aside for a clean start (no stale checkpoint resume).
#    Skip if already archived this invocation (idempotent re-launch).
if [ -d "$TARGET/.codeprobe/runs" ]; then
    mv "$TARGET/.codeprobe/runs" "$TARGET/.codeprobe/runs.bak.pre-9tk-$STAMP"
    echo "archived stale runs/ -> runs.bak.pre-9tk-$STAMP"
fi

# 4. Install our 3-arm config.
cp "$INVDIR/experiment-9tk.json" "$TARGET/.codeprobe/experiment.json"
mkdir -p "$LOGDIR"
STDOUT_LOG="$LOGDIR/run-$STAMP.stdout.log"
STDERR_LOG="$LOGDIR/run-$STAMP.stderr.log"

# 5. Launch detached (setsid: survives launcher-session killpg).
cd "$TARGET"
setsid nohup codeprobe run . \
    --suite "$SUITE" \
    --repeats 6 \
    --parallel 2 \
    --config-parallel 1 \
    --max-cost-usd 500 \
    --tenant codeprobe-9tk \
    --force-plain \
    > "$STDOUT_LOG" 2> "$STDERR_LOG" &
PID=$!
echo "launched pid $PID (logs: $STDOUT_LOG)"

# 6. Verify the run came up and loaded all three arms before declaring success.
sleep 25
if ! kill -0 "$PID" 2>/dev/null; then
    echo "ABORT: run process died within 25s; stderr tail:" >&2
    tail -30 "$STDERR_LOG" >&2
    exit 1
fi
echo "run alive at pid $PID; stdout head:"
head -20 "$STDOUT_LOG" || true
