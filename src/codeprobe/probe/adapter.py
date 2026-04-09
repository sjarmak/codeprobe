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
from codeprobe.probe.writer import _TEST_SH_TEMPLATE  # reuse, don't duplicate

logger = logging.getLogger(__name__)


# Test script template imported from probe/writer.py — single source of truth.


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
