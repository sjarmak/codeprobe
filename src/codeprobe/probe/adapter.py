"""Adapt Probe objects to the standard eval Task directory layout.

Produces task directories with task_type=micro_probe and
verification_mode=artifact_eval metadata, plus a ground_truth.json
format that includes confidence and provenance fields.

The Probe dataclass (probe/generator.py) is NOT modified.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from codeprobe.probe.generator import Probe

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Test script template (same verifier as writer.py)
# ---------------------------------------------------------------------------

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


class ProbeTaskAdapter:
    """Convert Probe objects to the standard eval Task directory layout.

    Adds ``task_type = "micro_probe"`` and ``verification_mode = "artifact_eval"``
    to task.toml metadata, and writes ground_truth.json with ``confidence`` and
    ``provenance`` fields.
    """

    @staticmethod
    def to_task_directory(
        probe: Probe,
        output_dir: Path,
        *,
        index: int = 0,
        repo_name: str | None = None,
    ) -> Path:
        """Write a single Probe as a Task directory and return its path.

        Args:
            probe: The Probe to convert.
            output_dir: Parent directory for all task directories.
            index: Numeric index for task-ID uniqueness.
            repo_name: Optional repository name for task.toml metadata.

        Returns:
            Path to the created task directory.
        """
        task_id = _make_task_id(probe.template_name, index)
        task_dir = output_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        tests_dir = task_dir / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)

        _write_instruction(task_dir, probe)
        _write_task_toml(task_dir, task_id, probe, repo_name)
        _write_test_sh(tests_dir)
        _write_ground_truth(tests_dir, probe)

        logger.debug("Created task directory: %s", task_dir)
        return task_dir

    @staticmethod
    def convert_batch(
        probes: list[Probe],
        output_dir: Path,
        repo_name: str | None = None,
    ) -> list[Path]:
        """Convert a list of probes, returning all created task directory paths."""
        output_dir.mkdir(parents=True, exist_ok=True)
        return [
            ProbeTaskAdapter.to_task_directory(
                probe,
                output_dir,
                index=i,
                repo_name=repo_name,
            )
            for i, probe in enumerate(probes)
        ]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make_task_id(template_name: str, index: int) -> str:
    """Build a deterministic task ID from template name and index."""
    short = re.sub(r"[^a-zA-Z0-9]", "", template_name)[:12]
    return f"probe-{short}-{index:03d}"


def _write_instruction(task_dir: Path, probe: Probe) -> None:
    (task_dir / "instruction.md").write_text(probe.prompt + "\n", encoding="utf-8")


def _write_task_toml(
    task_dir: Path,
    task_id: str,
    probe: Probe,
    repo_name: str | None,
) -> None:
    repo_line = f'repo = "{repo_name}"\n' if repo_name else ""
    tags = ", ".join(f'"{t}"' for t in probe.capability_tags)
    content = f"""\
version = "1.0"

[metadata]
name = "{task_id}"
difficulty = "{probe.difficulty}"
description = "Micro-benchmark probe: {probe.template_name}"
task_type = "micro_probe"
capability_tags = [{tags}]

[task]
id = "{task_id}"
name = "{task_id}"
{repo_line}category = "{probe.category}"
time_limit_sec = {probe.time_limit_sec}

[verification]
type = "test"
mode = "artifact_eval"
command = "bash tests/test.sh"
reward_type = "exact_match"
"""
    (task_dir / "task.toml").write_text(content, encoding="utf-8")


def _write_test_sh(tests_dir: Path) -> None:
    test_sh = tests_dir / "test.sh"
    test_sh.write_text(_TEST_SH_TEMPLATE, encoding="utf-8")
    test_sh.chmod(0o755)


def _write_ground_truth(tests_dir: Path, probe: Probe) -> None:
    ground_truth = {
        "answer": probe.answer,
        "answer_type": probe.answer_type,
        "confidence": 1.0,
        "provenance": "deterministic",
    }
    (tests_dir / "ground_truth.json").write_text(
        json.dumps(ground_truth, indent=2) + "\n",
        encoding="utf-8",
    )
