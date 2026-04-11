"""Write generated probe tasks to the standard eval task directory format.

Ported from ~/MCP-Eval-Tasks/scripts/probe_writer.py with adaptations
for the codeprobe package structure.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from codeprobe.probe.generator import Probe

logger = logging.getLogger(__name__)


def write_probe_tasks(
    probes: list[Probe],
    output_dir: Path,
    repo_name: str | None = None,
) -> list[Path]:
    """Write probe tasks to the standard task directory format.

    Each probe becomes a task directory with instruction.md, task.toml,
    tests/test.sh, and tests/ground_truth.json.

    Returns list of created task directory paths.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []

    for i, probe in enumerate(probes):
        short_name = re.sub(r"[^a-zA-Z0-9]", "", probe.template_name)[:12]
        task_id = f"probe-{short_name}-{i:03d}"
        task_dir = output_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        tests_dir = task_dir / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)

        _write_task_toml(task_dir, task_id, probe, repo_name)
        _write_instruction(task_dir, probe)
        _write_test_sh(tests_dir, probe)
        _write_ground_truth(tests_dir, probe)

        created.append(task_dir)

    return created


def _write_task_toml(
    task_dir: Path,
    task_id: str,
    probe: Probe,
    repo_name: str | None,
) -> None:
    """Write task.toml for a probe task."""
    repo_line = f'repo = "{repo_name}"\n' if repo_name else ""
    content = f"""\
version = "1.0"

[metadata]
name = "{task_id}"
difficulty = "{probe.difficulty}"
description = "Micro-benchmark probe: {probe.template_name}"

[task]
id = "{task_id}"
name = "{task_id}"
{repo_line}category = "{probe.category}"
time_limit_sec = {probe.time_limit_sec}

[verification]
type = "test"
command = "bash tests/test.sh"
reward_type = "exact_match"
"""
    (task_dir / "task.toml").write_text(content, encoding="utf-8")


def _write_instruction(task_dir: Path, probe: Probe) -> None:
    """Write instruction.md for a probe task."""
    (task_dir / "instruction.md").write_text(probe.prompt + "\n", encoding="utf-8")


def _write_test_sh(tests_dir: Path, probe: Probe) -> None:
    """Write tests/test.sh — exact-match verifier using ground_truth.json."""
    content = _TEST_SH_TEMPLATE
    test_sh = tests_dir / "test.sh"
    test_sh.write_text(content, encoding="utf-8")
    test_sh.chmod(0o755)


def _write_ground_truth(tests_dir: Path, probe: Probe) -> None:
    """Write tests/ground_truth.json with the expected answer."""
    ground_truth = {
        "answer": probe.answer,
        "answer_type": probe.answer_type,
        "template": probe.template_name,
        "capability_tags": list(probe.capability_tags),
    }
    (tests_dir / "ground_truth.json").write_text(
        json.dumps(ground_truth, indent=2) + "\n", encoding="utf-8"
    )


_TEST_SH_TEMPLATE = """\
#!/usr/bin/env bash
set -euo pipefail

# Probe verification script — compares agent output against ground truth.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GT_FILE="$SCRIPT_DIR/ground_truth.json"

if [ ! -f "$GT_FILE" ]; then
    echo "FAIL: ground_truth.json not found"
    exit 1
fi

EXPECTED=$(python3 -c "import json; print(json.load(open('$GT_FILE'))['answer'])")
ANSWER_TYPE=$(python3 -c "import json; print(json.load(open('$GT_FILE'))['answer_type'])")

# Read agent output: $AGENT_OUTPUT (sandbox), answer.txt (manual), or stdin
if [ -n "${AGENT_OUTPUT:-}" ] && [ -f "$AGENT_OUTPUT" ]; then
    ACTUAL=$(cat "$AGENT_OUTPUT")
elif [ -f answer.txt ]; then
    ACTUAL=$(cat answer.txt)
else
    ACTUAL=$(cat)
fi

# Trim whitespace
ACTUAL=$(echo "$ACTUAL" | xargs)
EXPECTED=$(echo "$EXPECTED" | xargs)

# Normalize based on answer type
case "$ANSWER_TYPE" in
    file_path)
        ACTUAL=$(echo "$ACTUAL" | sed 's|\\\\\\\\|/|g; s|^\\\\./||')
        EXPECTED=$(echo "$EXPECTED" | sed 's|\\\\\\\\|/|g; s|^\\\\./||')
        ACTUAL_LOWER=$(echo "$ACTUAL" | tr '[:upper:]' '[:lower:]')
        EXPECTED_LOWER=$(echo "$EXPECTED" | tr '[:upper:]' '[:lower:]')
        if [ "$ACTUAL_LOWER" = "$EXPECTED_LOWER" ]; then
            echo "PASS: $ACTUAL"
            exit 0
        fi
        ;;
    integer)
        ACTUAL_INT=$(echo "$ACTUAL" | grep -oE '[0-9]+' | head -1)
        if [ "$ACTUAL_INT" = "$EXPECTED" ]; then
            echo "PASS: $ACTUAL_INT"
            exit 0
        fi
        ACTUAL="$ACTUAL_INT"
        ;;
    boolean)
        ACTUAL_BOOL=$(echo "$ACTUAL" | tr '[:upper:]' '[:lower:]')
        case "$ACTUAL_BOOL" in
            yes|true|1) ACTUAL_BOOL="yes" ;;
            no|false|0) ACTUAL_BOOL="no" ;;
        esac
        EXPECTED_BOOL=$(echo "$EXPECTED" | tr '[:upper:]' '[:lower:]')
        if [ "$ACTUAL_BOOL" = "$EXPECTED_BOOL" ]; then
            echo "PASS: $ACTUAL_BOOL"
            exit 0
        fi
        ACTUAL="$ACTUAL_BOOL"
        EXPECTED="$EXPECTED_BOOL"
        ;;
    text|*)
        ACTUAL_NORM=$(echo "$ACTUAL" | tr '[:upper:]' '[:lower:]' | tr -s ' ')
        EXPECTED_NORM=$(echo "$EXPECTED" | tr '[:upper:]' '[:lower:]' | tr -s ' ')
        if [ "$ACTUAL_NORM" = "$EXPECTED_NORM" ]; then
            echo "PASS: $ACTUAL"
            exit 0
        fi
        ;;
esac

echo "FAIL: expected='$EXPECTED' actual='$ACTUAL'"
exit 1
"""
