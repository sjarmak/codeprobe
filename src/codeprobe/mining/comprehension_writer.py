"""Writer for comprehension tasks — produces task directories on disk.

Separated from ``comprehension.py`` to keep file sizes manageable.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

from codeprobe.mining.comprehension import _TASK_SPECS, ComprehensionTaskSpec
from codeprobe.mining.writer import _write_checkpoints
from codeprobe.models.task import Task

logger = logging.getLogger(__name__)


# Direct (visible) leg for a dual comprehension task: pass iff the agent
# wrote a parseable, non-empty ``answer.json`` at the repository root. The
# held-out artifact leg (answer.json vs tests/ground_truth.json) is scored
# independently by the ArtifactScorer. Failures are SILENT on stderr — the
# executor's BinaryScorer surfaces non-empty stderr as an errored leg, and
# an errored leg gets the whole trial excluded downstream (AOA R0 gate)
# instead of counted as a clean failure.
_DUAL_DIRECT_LEG_TEMPLATE = """\
#!/usr/bin/env bash
# Direct (visible) leg for dual comprehension task {task_id}.
# Passes iff the agent wrote a parseable, non-empty answer.json at the
# repository root. Gameable by design — the held-out artifact leg does
# the real oracle comparison. Failures stay silent on stderr so a
# legitimate miss is a clean passed_direct=false, not an errored leg.
cd "${{TASK_REPO_ROOT:-{repo_default}}}" 2>/dev/null || exit 1
[ -s answer.json ] || exit 1
python3 -c 'import json; json.load(open("answer.json"))' 2>/dev/null || exit 1
exit 0
"""


def write_comprehension_tasks(
    tasks: list[Task],
    output_dir: Path,
    specs: dict[str, ComprehensionTaskSpec] | None = None,
    *,
    repo_path: Path | None = None,
    commit: str | None = None,
    divergence_reports: dict[str, dict] | None = None,
) -> list[Path]:
    """Write comprehension tasks to disk with the new ground_truth format.

    Produces::

        output_dir/<task.id>/
            instruction.md
            metadata.json
            divergence_report.json (consensus-verified tasks only)
            tests/ground_truth.json
            tests/test.sh          (dual tasks only — the direct leg)

    Ground truth JSON::

        {
          "answer": ...,
          "answer_type": "file_list" | "count" | "boolean" | "text",
          "confidence": 0.95,
          "provenance": "deterministic",
          "commit": "<mine-time HEAD>"   (only when *commit* is given)
        }

    Tasks with ``verification_mode="dual"`` (produced by
    ``ComprehensionGenerator.generate(dual=True)`` on the ``mine
    --dual-verify`` path) additionally get a direct-leg ``tests/test.sh``;
    ``repo_path`` supplies the ``TASK_REPO_ROOT`` fallback default baked
    into that script and is required for dual tasks.

    *commit* is the mine-time HEAD recorded in ``ground_truth.json``. It
    deliberately does NOT touch ``metadata.ground_truth_commit`` — that
    field pins executor workspaces (and gets rewritten by the R0 driver's
    expB anchoring); this one is provenance-only, read by aoa-bench.

    *divergence_reports* maps task id -> the consensus.v1 report from
    :func:`codeprobe.mining.comprehension_consensus.verify_comprehension_tasks`;
    each is written to ``<task_dir>/divergence_report.json``, the record
    that makes the task's held-out provenance NativeComposed downstream.

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

        is_dual = task.verification.verification_mode == "dual"
        if is_dual and repo_path is None:
            raise ValueError(
                f"task {task.id}: dual comprehension tasks require repo_path "
                "for the direct-leg TASK_REPO_ROOT fallback"
            )

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
        if commit:
            ground_truth["commit"] = commit
        (tests_dir / "ground_truth.json").write_text(
            json.dumps(ground_truth, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        report = (divergence_reports or {}).get(task.id)
        if report is not None:
            (task_dir / "divergence_report.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

        if is_dual:
            test_sh = tests_dir / "test.sh"
            test_sh.write_text(
                _DUAL_DIRECT_LEG_TEMPLATE.format(
                    task_id=safe_id, repo_default=repo_path
                ),
                encoding="utf-8",
            )
            test_sh.chmod(0o755)

        # R17: multi-step templates (import_chain, dependency_analysis)
        # attach checkpoints; the writer resolves the script bodies from
        # the task category. No-op for single-step templates.
        _write_checkpoints(task, tests_dir, None)

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
