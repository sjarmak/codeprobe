#!/bin/bash
# Smoke fixture oracle for codeprobe-2nw2 scaffold mode.
#
# Passes if the agent's edit to src/math.go is visible in the merged
# workspace state — i.e. the scaffold mode's __exit__ overlay step
# successfully copied the agent's version over the restored source.
#
# Intentionally does NOT run `go test`; the fixture is a structural
# smoke harness, not a real Go project.
set -euo pipefail

REPO_ROOT="${TASK_REPO_ROOT:-$(pwd)}"

grep -q "func add" "$REPO_ROOT/src/math.go" && exit 0 || exit 1
