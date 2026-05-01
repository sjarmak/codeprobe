#!/usr/bin/env bash
# Demo verifier — partial credit when at least one error branch is mentioned.
matches=$(grep -c "raise\|except\|error" "${AGENT_OUTPUT:-/dev/null}" 2>/dev/null || echo 0)
if [ "$matches" -ge 3 ]; then
    echo '{"score": 1.0, "passed": true}'
    exit 0
elif [ "$matches" -ge 1 ]; then
    echo '{"score": 0.5, "passed": false}'
    exit 0
fi
echo '{"score": 0.0, "passed": false}'
exit 1
