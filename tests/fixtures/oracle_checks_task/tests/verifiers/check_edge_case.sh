#!/usr/bin/env bash
# Demo verifier — passes only when AGENT_OUTPUT mentions the edge case.
# Real tasks would inspect the patched source / answer.json instead.
if grep -q "edge_case_x" "${AGENT_OUTPUT:-/dev/null}" 2>/dev/null; then
    echo '{"score": 1.0, "passed": true}'
    exit 0
fi
echo '{"score": 0.0, "passed": false}'
exit 1
