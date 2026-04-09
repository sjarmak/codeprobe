"""Writer for comprehension tasks — produces task directories on disk.

Separated from ``comprehension.py`` to keep file sizes manageable.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

from codeprobe.mining.comprehension import ComprehensionTaskSpec, _TASK_SPECS
from codeprobe.models.task import Task

logger = logging.getLogger(__name__)


def write_comprehension_tasks(
    tasks: list[Task],
    output_dir: Path,
    specs: dict[str, ComprehensionTaskSpec] | None = None,
) -> list[Path]:
    """Write comprehension tasks to disk with the new ground_truth format.

    Produces::

        output_dir/<task.id>/
            instruction.md
            metadata.json
            tests/ground_truth.json

    Ground truth JSON::

        {
          "answer": ...,
          "answer_type": "file_list" | "count" | "boolean" | "text",
          "confidence": 0.95,
          "provenance": "deterministic"
        }

    Tasks must have been produced by ``ComprehensionGenerator.generate`` --
    the spec is looked up from a process-wide registry keyed on ``task.id``.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    registry = specs if specs is not None else _TASK_SPECS
    for task in tasks:
        spec = registry.get(task.id)
        if spec is None:
            logger.warning("No spec registered for task %s, skipping", task.id)
            continue

        safe_id = Path(task.id).name
        if not safe_id or safe_id != task.id:
            raise ValueError(f"Invalid task id for filesystem use: {task.id!r}")

        task_dir = output_dir / safe_id
        tests_dir = task_dir / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)

        instruction = _build_instruction(task, spec)
        (task_dir / "instruction.md").write_text(instruction, encoding="utf-8")

        metadata_payload = asdict(task)
        (task_dir / "metadata.json").write_text(
            json.dumps(metadata_payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        ground_truth = {
            "answer": spec.answer,
            "answer_type": spec.answer_type,
            "confidence": spec.confidence,
            "provenance": spec.provenance,
        }
        (tests_dir / "ground_truth.json").write_text(
            json.dumps(ground_truth, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        written.append(task_dir)
        logger.info("Wrote comprehension task %s -> %s", task.id, task_dir)

    return written


def _build_instruction(task: Task, spec: ComprehensionTaskSpec) -> str:
    """Render instruction.md for a comprehension task."""
    answer_format = {
        "file_list": (
            "Return a JSON array of file paths (strings) relative to the "
            "repository root, sorted lexicographically."
        ),
        "boolean": "Answer with the single word `true` or `false`.",
        "text": "Return only the exact text, with no extra commentary.",
        "count": "Return only a single integer.",
    }.get(spec.answer_type, "Provide your answer.")

    return (
        f"# {task.metadata.name}\n\n"
        f"**Repository:** {task.repo}\n"
        f"**Task type:** {task.metadata.task_type}\n"
        f"**Template:** {spec.template}\n\n"
        "## Question\n\n"
        f"{spec.question}\n\n"
        "## Answer Format\n\n"
        f"{answer_format}\n\n"
        "Write your answer to `answer.json` in the repository root.\n"
        'For file lists: `{"answer": ["path/a.py", "path/b.py"]}`\n'
        'For other types: `{"answer": "your answer"}`\n'
    )
