#!/bin/bash
# answer_json_verifier_lib.sh — CSB (Composite Shell Benchmark) compatibility shim.
#
# This library is a thin wrapper that delegates artifact verification to the
# Python ArtifactScorer exposed via:
#
#     python3 -m codeprobe.core.scoring --artifact <task_dir>
#
# It exists purely so test.sh files in dual-mode (CSB-compatible) tasks can
# source a shell function with the same name and shape they expect, without
# duplicating any verification logic in shell. All real scoring happens in
# codeprobe.core.scoring.ArtifactScorer.
#
# Constraints (intentional):
#   - No jq dependency (parses the JSON score via python3 -c).
#   - No bash 4+ features (no associative arrays, no ${!var}, no [[ =~ ]]).
#   - Must work on bash 3.2 (macOS system default).
#
# This file is meant to be SOURCED, not executed directly.

# ---------------------------------------------------------------------------
# Source guard — idempotent sourcing.
# ---------------------------------------------------------------------------
if [ "${CODEPROBE_ANSWER_JSON_VERIFIER_LIB_SOURCED:-0}" = "1" ]; then
    return 0 2>/dev/null || exit 0
fi
CODEPROBE_ANSWER_JSON_VERIFIER_LIB_SOURCED=1

# ---------------------------------------------------------------------------
# validate_answer_json <task_dir>
#
# Delegates to the Python ArtifactScorer. Captures the JSON result, extracts
# the numeric score, writes it to <task_dir>/reward.txt, and returns the
# Python process's exit code.
#
# Arguments:
#   $1 — task directory containing ground_truth.json and answer.json
#
# Side effects:
#   - Writes <task_dir>/reward.txt with a float score (e.g. "1.0").
#   - Prints the raw JSON result from the Python scorer to stdout.
#
# Returns:
#   The exit code of `python3 -m codeprobe.core.scoring --artifact`.
# ---------------------------------------------------------------------------
validate_answer_json() {
    local task_dir="$1"

    if [ -z "$task_dir" ]; then
        echo "validate_answer_json: missing task_dir argument" >&2
        return 2
    fi

    if [ ! -d "$task_dir" ]; then
        echo "validate_answer_json: not a directory: $task_dir" >&2
        return 2
    fi

    local scorer_output
    local scorer_rc
    # -W ignore suppresses the runpy RuntimeWarning so stdout stays clean JSON.
    scorer_output=$(python3 -W ignore -m codeprobe.core.scoring --artifact "$task_dir" 2>&1)
    scorer_rc=$?

    # Always surface the scorer output for test.sh logs.
    printf '%s\n' "$scorer_output"

    if [ "$scorer_rc" -ne 0 ]; then
        return "$scorer_rc"
    fi

    # Extract the "score" field from the JSON result using python3 (no jq).
    # We parse line-by-line and pick the first line that is valid JSON with a
    # numeric "score" field — this is robust against stray warnings or log
    # lines that may be mixed into stdout. Writes reward.txt with the numeric
    # score so downstream ContinuousScorer fallbacks can pick it up.
    local score
    score=$(
        printf '%s' "$scorer_output" | python3 -c '
import json, sys
for line in sys.stdin.read().splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        data = json.loads(line)
    except Exception:
        continue
    if not isinstance(data, dict):
        continue
    s = data.get("score")
    if s is None:
        continue
    try:
        print(float(s))
    except (TypeError, ValueError):
        sys.exit(1)
    sys.exit(0)
sys.exit(1)
'
    )
    local extract_rc=$?

    if [ "$extract_rc" -ne 0 ] || [ -z "$score" ]; then
        echo "validate_answer_json: failed to parse score from scorer output" >&2
        return 1
    fi

    printf '%s\n' "$score" > "$task_dir/reward.txt"
    return 0
}
