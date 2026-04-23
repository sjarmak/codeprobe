"""Tests for R17 per-checkpoint script emission in mining.writer.

Covers:
- A mined ``change-scope-audit`` task writes ≥2 ``tests/verifiers/*.sh``
  scripts and a ``tests/checkpoints.json`` manifest.
- Multi-step comprehension templates emit their own 2-step checkpoints.
- Single-step tasks (plain ``sdlc_code_change``, ``return_type_resolution``
  comprehension) do NOT emit ``tests/verifiers/`` or
  ``tests/checkpoints.json`` — checkpoint emission is gated on
  ``task.verification.checkpoints``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeprobe.mining.comprehension import (
    COMPREHENSION_CHECKPOINT_SCRIPTS,
    _comprehension_checkpoints,
)
from codeprobe.mining.org_scale import (
    CHANGE_SCOPE_CHECKPOINT_SCRIPTS,
    _change_scope_checkpoints,
)
from codeprobe.mining.writer import (
    resolve_checkpoint_scripts,
    write_task_dir,
)
from codeprobe.models.task import (
    Checkpoint,
    Task,
    TaskMetadata,
    TaskVerification,
)


def _make_change_scope_task() -> Task:
    return Task(
        id="change-scope-abc12345",
        repo="example/repo",
        metadata=TaskMetadata(
            name="org-change-scope-abc12345",
            difficulty="hard",
            description="change-scope-audit: Foo (5 files)",
            language="python",
            category="change-scope-audit",
            org_scale=True,
            issue_title="Blast radius of changing Foo",
            issue_body="Find all files that depend on Foo.",
            ground_truth_commit="deadbeef",
        ),
        verification=TaskVerification(
            type="oracle",
            command="bash tests/test.sh",
            reward_type="continuous",
            oracle_type="file_list",
            oracle_answer=("src/a.py", "src/b.py"),
            checkpoints=_change_scope_checkpoints(),
        ),
    )


def _make_plain_sdlc_task() -> Task:
    """A single-step sdlc_code_change task with no checkpoints."""
    return Task(
        id="sdlc-plain-001",
        repo="example/repo",
        metadata=TaskMetadata(
            name="sdlc-plain-001",
            category="sdlc",
            task_type="sdlc_code_change",
            issue_title="Fix bug",
            issue_body="Fix the thing.",
            language="python",
        ),
        verification=TaskVerification(
            type="test_script",
            command="bash tests/test.sh",
            reward_type="binary",
        ),
    )


class TestChangeScopeAuditEmitsCheckpoints:
    """A mined change-scope-audit task ships ≥2 per-checkpoint scripts."""

    def test_writes_two_verifier_scripts(self, tmp_path: Path) -> None:
        task = _make_change_scope_task()
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        base_dir = tmp_path / "tasks"
        base_dir.mkdir()

        task_dir = write_task_dir(task, base_dir, repo_path)

        verifiers_dir = task_dir / "tests" / "verifiers"
        assert verifiers_dir.is_dir(), "tests/verifiers/ must be emitted"

        scripts = sorted(p.name for p in verifiers_dir.iterdir() if p.is_file())
        assert len(scripts) >= 2, f"expected ≥2 verifier scripts, got {scripts}"
        assert "step1_answer_provided.sh" in scripts
        assert "step2_scope_correct.sh" in scripts

        # Scripts are executable (chmod 0o755).
        for name in scripts:
            path = verifiers_dir / name
            assert path.stat().st_mode & 0o111, f"{name} must be executable"
            # Each script must have real content (not a stub).
            body = path.read_text()
            assert "#!/usr/bin/env bash" in body
            assert body.strip().splitlines()[-1] != "exit 0" or "answer" in body

    def test_writes_checkpoints_manifest(self, tmp_path: Path) -> None:
        task = _make_change_scope_task()
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        base_dir = tmp_path / "tasks"
        base_dir.mkdir()

        task_dir = write_task_dir(task, base_dir, repo_path)

        manifest = task_dir / "tests" / "checkpoints.json"
        assert manifest.is_file()
        data = json.loads(manifest.read_text())
        assert isinstance(data, list)
        assert len(data) == 2
        for entry in data:
            assert {"name", "weight", "verifier"} <= set(entry)
        # Weights sum to 1.0 (CheckpointScorer will reject otherwise).
        total = sum(float(entry["weight"]) for entry in data)
        assert total == pytest.approx(1.0)

    def test_scripts_match_checkpoint_manifest(self, tmp_path: Path) -> None:
        task = _make_change_scope_task()
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        base_dir = tmp_path / "tasks"
        base_dir.mkdir()

        task_dir = write_task_dir(task, base_dir, repo_path)

        manifest = json.loads(
            (task_dir / "tests" / "checkpoints.json").read_text()
        )
        verifiers_dir = task_dir / "tests" / "verifiers"
        for entry in manifest:
            assert (verifiers_dir / entry["verifier"]).is_file()


class TestSingleStepTasksHaveNoCheckpoints:
    """Gate: tasks without checkpoints must not emit the checks/ layout."""

    def test_plain_sdlc_task_no_checks_dir(self, tmp_path: Path) -> None:
        task = _make_plain_sdlc_task()
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        base_dir = tmp_path / "tasks"
        base_dir.mkdir()

        task_dir = write_task_dir(task, base_dir, repo_path)

        assert not (task_dir / "tests" / "verifiers").exists(), (
            "single-step tasks must NOT emit tests/verifiers/"
        )
        assert not (task_dir / "tests" / "checkpoints.json").exists(), (
            "single-step tasks must NOT emit tests/checkpoints.json"
        )

    def test_empty_checkpoints_tuple_no_emission(self, tmp_path: Path) -> None:
        """Explicit empty ``checkpoints=()`` still yields no checks layout."""
        task = Task(
            id="oracle-no-cp-001",
            repo="example/repo",
            metadata=TaskMetadata(
                name="oracle-no-cp-001",
                category="migration-inventory",
                language="python",
                org_scale=True,
                issue_title="Find deprecated APIs",
                issue_body="List files.",
                ground_truth_commit="abc",
            ),
            verification=TaskVerification(
                type="oracle",
                command="bash tests/test.sh",
                reward_type="continuous",
                oracle_type="file_list",
                oracle_answer=("src/a.py",),
                checkpoints=(),
            ),
        )
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        base_dir = tmp_path / "tasks"
        base_dir.mkdir()

        task_dir = write_task_dir(task, base_dir, repo_path)

        assert not (task_dir / "tests" / "verifiers").exists()
        assert not (task_dir / "tests" / "checkpoints.json").exists()


class TestComprehensionCheckpoints:
    """Multi-step comprehension templates emit their own 2-step checkpoints."""

    def test_import_chain_gets_checkpoints(self) -> None:
        # Structural check on the generator-level contract: the multi-step
        # template set is exported and non-empty, and helpers return exactly
        # two checkpoints with weights summing to 1.0.
        from codeprobe.mining.comprehension import _MULTI_STEP_TEMPLATES

        assert "import_chain" in _MULTI_STEP_TEMPLATES
        assert "dependency_analysis" in _MULTI_STEP_TEMPLATES

        cps = _comprehension_checkpoints()
        assert len(cps) == 2
        assert sum(cp.weight for cp in cps) == pytest.approx(1.0)
        # Scripts must exist for every declared verifier name.
        for cp in cps:
            assert cp.verifier in COMPREHENSION_CHECKPOINT_SCRIPTS

    def test_change_scope_scripts_exhaustive(self) -> None:
        """Every checkpoint verifier has a script body registered."""
        cps = _change_scope_checkpoints()
        for cp in cps:
            assert cp.verifier in CHANGE_SCOPE_CHECKPOINT_SCRIPTS
            assert CHANGE_SCOPE_CHECKPOINT_SCRIPTS[cp.verifier].startswith(
                "#!/usr/bin/env bash"
            )

    def test_step2_scope_correct_pins_pass_threshold(self) -> None:
        """Regression: ``step2_scope_correct.sh`` must embed the live
        ``PASS_THRESHOLD`` from :mod:`codeprobe.analysis.stats`.

        Previously the shell script baked in a literal ``0.5``. That
        coincidentally matches the current threshold but would silently
        drift if ``PASS_THRESHOLD`` changed — emitted checkpoints would
        disagree with the runtime scorer.
        """
        from codeprobe.analysis.stats import PASS_THRESHOLD

        script = CHANGE_SCOPE_CHECKPOINT_SCRIPTS["step2_scope_correct.sh"]
        # The pinned value must appear as the awk threshold comparison.
        assert f">= {PASS_THRESHOLD}" in script, (
            "step2_scope_correct.sh should embed the current PASS_THRESHOLD "
            "so the emitted script tracks the mine-time threshold."
        )


class TestResolveCheckpointScripts:
    def test_returns_none_for_empty_checkpoints(self) -> None:
        task = _make_plain_sdlc_task()
        assert resolve_checkpoint_scripts(task) is None

    def test_returns_change_scope_scripts(self) -> None:
        task = _make_change_scope_task()
        scripts = resolve_checkpoint_scripts(task)
        assert scripts is not None
        assert "step1_answer_provided.sh" in scripts
        assert "step2_scope_correct.sh" in scripts


class TestExplicitCheckpointScripts:
    """Caller can override the built-in scripts via kwarg."""

    def test_custom_scripts_used(self, tmp_path: Path) -> None:
        task = Task(
            id="custom-cp-001",
            repo="example/repo",
            metadata=TaskMetadata(
                name="custom-cp-001",
                category="sdlc",
                task_type="sdlc_code_change",
                issue_title="X",
                issue_body="Y",
                language="python",
            ),
            verification=TaskVerification(
                type="test_script",
                command="bash tests/test.sh",
                reward_type="checkpoint",
                checkpoints=(
                    Checkpoint(name="a", weight=0.5, verifier="a.sh"),
                    Checkpoint(name="b", weight=0.5, verifier="b.sh"),
                ),
            ),
        )
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        base_dir = tmp_path / "tasks"
        base_dir.mkdir()

        custom_scripts = {
            "a.sh": "#!/usr/bin/env bash\necho MARKER-A\nexit 0\n",
            "b.sh": "#!/usr/bin/env bash\necho MARKER-B\nexit 0\n",
        }
        task_dir = write_task_dir(
            task,
            base_dir,
            repo_path,
            checkpoint_scripts=custom_scripts,
        )

        a_body = (task_dir / "tests" / "verifiers" / "a.sh").read_text()
        b_body = (task_dir / "tests" / "verifiers" / "b.sh").read_text()
        assert "MARKER-A" in a_body
        assert "MARKER-B" in b_body
