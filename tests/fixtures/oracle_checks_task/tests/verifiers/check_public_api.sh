#!/usr/bin/env bash
# Demo verifier — passes when AGENT_OUTPUT does NOT contain breaking-change markers.
if grep -q "BREAKING\|removed_public_api" "${AGENT_OUTPUT:-/dev/null}" 2>/dev/null; then
    echo '{"score": 0.0, "passed": false}'
    exit 1
fi
echo '{"score": 1.0, "passed": true}'
exit 0
