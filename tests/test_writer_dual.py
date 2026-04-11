"""Tests for dual-verification task layout emitted by write_task_dir()."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from codeprobe.mining.writer import write_task_dir
from codeprobe.models.task import Task, TaskMetadata, TaskVerification


def _make_dual_task(
    task_id: str = "dual0001",
    *,
    with_issue: bool = True,
) -> Task:
    """Construct a minimal Task with verification_mode='dual'."""
    metadata = TaskMetadata(
        name=f"merge-{task_id}",
        difficulty="medium",
        description="PR title: add dual verification support\n\nBody paragraph.",
        language="python",
        issue_title="Auth tokens expire silently" if with_issue else "",
        issue_body=(
            (
                "Users report that auth tokens expire without warning.\n\n"
                "Repro:\n"
                "1. Log in\n"
                "2. Wait 1 hour\n"
                "3. Make an API call — silent 401\n"
            )
            if with_issue
            else ""
        ),
    )
    verification = TaskVerification(
        type="test_script",
        command="bash tests/test.sh",
        verification_mode="dual",
        scoring_policy="weighted",
        weight_direct=0.6,
        weight_artifact=0.4,
        reward_type="binary",
    )
    return Task(
        id=task_id,
        repo="myrepo",
        metadata=metadata,
        verification=verification,
    )


class TestWriteTaskDirDualLayout:
    """Acceptance tests for u7-writer-dual-layout."""

    def test_creates_all_four_files(self, tmp_path: Path) -> None:
        """AC1: dual mode creates test.sh, ground_truth.json, instruction.md, metadata.json."""
        task = _make_dual_task()
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "myrepo"

        result_path = write_task_dir(task, base_dir, repo_path)

        assert result_path == base_dir / "dual0001"
        assert result_path.is_dir()

        assert (result_path / "instruction.md").is_file()
        assert (result_path / "tests" / "test.sh").is_file()
        assert (result_path / "tests" / "ground_truth.json").is_file()
        assert (result_path / "metadata.json").is_file()

    def test_instruction_has_answer_json_schema_section(self, tmp_path: Path) -> None:
        """AC2: instruction.md has an 'Expected answer.json' section listing required fields."""
        task = _make_dual_task()
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "myrepo"

        result_path = write_task_dir(task, base_dir, repo_path)
        instruction = (result_path / "instruction.md").read_text(encoding="utf-8")

        # Section header present
        assert "## Expected answer.json" in instruction
        # Required fields listed
        assert "answer_type" in instruction
        assert "`answer`" in instruction or "answer`:" in instruction
        # Answer type options enumerated
        assert "file_list" in instruction
        assert "count" in instruction
        assert "boolean" in instruction
        assert "text" in instruction

    def test_instruction_states_both_scored(self, tmp_path: Path) -> None:
        """AC3: instruction.md explicitly states both code changes and artifact are evaluated."""
        task = _make_dual_task()
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "myrepo"

        result_path = write_task_dir(task, base_dir, repo_path)
        instruction = (result_path / "instruction.md").read_text(encoding="utf-8")

        # "BOTH ... will be evaluated"
        assert "BOTH" in instruction
        # Direct verification explicitly mentioned
        assert (
            "Direct verification" in instruction or "direct verification" in instruction
        )
        assert "tests/test.sh" in instruction
        # Artifact evaluation explicitly mentioned
        assert "answer.json" in instruction
        assert "Artifact evaluation" in instruction or "artifact" in instruction.lower()

    def test_instruction_is_single_combined_prompt(self, tmp_path: Path) -> None:
        """PRD Open Q#5: single combined prompt, not two separate sections."""
        task = _make_dual_task()
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "myrepo"

        result_path = write_task_dir(task, base_dir, repo_path)
        instruction = (result_path / "instruction.md").read_text(encoding="utf-8")

        # Exactly one top-level H1 (the task title)
        h1_lines = [
            ln
            for ln in instruction.splitlines()
            if ln.startswith("# ") and not ln.startswith("## ")
        ]
        assert len(h1_lines) == 1, f"Expected 1 H1, got {len(h1_lines)}: {h1_lines}"

    def test_test_sh_is_executable(self, tmp_path: Path) -> None:
        """AC4: generated test.sh is executable (mode 0755)."""
        task = _make_dual_task()
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "myrepo"

        result_path = write_task_dir(task, base_dir, repo_path)
        test_sh = result_path / "tests" / "test.sh"

        assert test_sh.is_file()
        assert os.access(test_sh, os.X_OK)
        mode = test_sh.stat().st_mode & 0o777
        assert mode == 0o755, f"Expected 0o755, got {oct(mode)}"

        content = test_sh.read_text(encoding="utf-8")
        assert content.startswith("#!/usr/bin/env bash")
        assert "set -euo pipefail" in content
        assert "bash tests/test.sh" in content

    def test_metadata_has_verification_mode_dual(self, tmp_path: Path) -> None:
        """AC5: metadata.json contains verification_mode='dual'."""
        task = _make_dual_task()
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "myrepo"

        result_path = write_task_dir(task, base_dir, repo_path)
        meta = json.loads((result_path / "metadata.json").read_text(encoding="utf-8"))

        assert meta["id"] == "dual0001"
        assert meta["verification"]["verification_mode"] == "dual"
        assert meta["verification"]["scoring_policy"] == "weighted"
        assert meta["verification"]["weight_direct"] == pytest.approx(0.6)
        assert meta["verification"]["weight_artifact"] == pytest.approx(0.4)

    def test_ground_truth_stub_is_valid_json(self, tmp_path: Path) -> None:
        """ground_truth.json stub is valid JSON with required schema fields."""
        task = _make_dual_task()
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "myrepo"

        result_path = write_task_dir(task, base_dir, repo_path)
        gt_path = result_path / "tests" / "ground_truth.json"
        gt = json.loads(gt_path.read_text(encoding="utf-8"))

        # Schema validity: answer_type + answer keys present (required by scoring.py new format)
        assert "answer_type" in gt
        assert "answer" in gt
        # Stub metadata for phase 2 mining
        assert "oracle_metadata" in gt
        assert gt["oracle_metadata"]["task_id"] == "dual0001"

    def test_fallback_instruction_without_issue(self, tmp_path: Path) -> None:
        """Dual mode still works for tasks without issue metadata."""
        task = _make_dual_task(task_id="dual0002", with_issue=False)
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "myrepo"

        result_path = write_task_dir(task, base_dir, repo_path)
        instruction = (result_path / "instruction.md").read_text(encoding="utf-8")

        # Fallback uses the task name as heading
        assert "# merge-dual0002" in instruction
        # Dual scoring section still present
        assert "## Expected answer.json" in instruction
        assert "BOTH" in instruction

    def test_rejects_invalid_command(self, tmp_path: Path) -> None:
        """Dual mode still enforces the command allowlist."""
        task = Task(
            id="dualbad01",
            repo="myrepo",
            metadata=TaskMetadata(name="merge-dualbad01", language="python"),
            verification=TaskVerification(
                type="test_script",
                command="rm -rf /",
                verification_mode="dual",
            ),
        )
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "myrepo"

        with pytest.raises(ValueError, match="not in allowlist"):
            write_task_dir(task, base_dir, repo_path)
