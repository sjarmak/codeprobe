"""Tests for weighted-checklist test.sh generation (bead codeprobe-br7.3)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from codeprobe.mining.writer import (
    _build_weighted_checklist_script,
    write_task_dir,
)
from codeprobe.models.task import Task, TaskMetadata, TaskVerification

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sdlc_task(
    task_id: str = "abc12345",
    language: str = "python",
    command: str = "pytest tests/test_auth.py",
) -> Task:
    metadata = TaskMetadata(
        name=f"merge-{task_id}",
        difficulty="medium",
        description="Fix auth",
        language=language,
        category="sdlc",
    )
    verification = TaskVerification(
        type="test_script",
        command=command,
        reward_type="binary",
    )
    return Task(id=task_id, repo="myrepo", metadata=metadata, verification=verification)


def _sdlc_gt(
    source_files: list[str] | None = None,
    changed_files: list[str] | None = None,
) -> dict:
    src = source_files if source_files is not None else ["src/auth.py"]
    changed = changed_files if changed_files is not None else list(src)
    return {
        "schema_version": "sdlc-v1",
        "changed_files": changed,
        "source_files": src,
        "test_files": [],
        "symbols": [],
        "diff_summary": "",
        "merge_sha": "abc1234567890abc",
        "populated_by": "mining-sdlc-ground-truth",
    }


# ---------------------------------------------------------------------------
# Tests: _build_weighted_checklist_script
# ---------------------------------------------------------------------------


class TestBuildWeightedChecklistScript:
    def test_emits_bash_shebang(self, tmp_path: Path) -> None:
        script = _build_weighted_checklist_script(
            cmd="pytest tests/",
            repo_path=tmp_path,
            language="python",
            ground_truth=_sdlc_gt(),
            header="hdr",
        )
        assert script.startswith("#!/usr/bin/env bash")

    def test_contains_four_weighted_sub_checks(self, tmp_path: Path) -> None:
        script = _build_weighted_checklist_script(
            cmd="pytest tests/",
            repo_path=tmp_path,
            language="python",
            ground_truth=_sdlc_gt(),
            header="hdr",
        )
        # Four sub-check names: correct_files, syntax_valid, scope_respected, test_passed
        assert "correct_files" in script
        assert "syntax_valid" in script
        assert "scope_respected" in script
        assert "test_passed" in script

    def test_weights_add_to_one(self, tmp_path: Path) -> None:
        script = _build_weighted_checklist_script(
            cmd="pytest tests/",
            repo_path=tmp_path,
            language="python",
            ground_truth=_sdlc_gt(),
            header="hdr",
        )
        # Ground-truth payload is JSON-embedded via a single-quoted env
        # var; extract it and check the weights arithmetic directly.
        match = re.search(r"CP_GROUND_TRUTH='([^']+)'", script)
        assert match is not None
        payload = json.loads(match.group(1))
        weights = payload["weights"]
        assert sorted(weights.keys()) == [
            "correct_files",
            "scope_respected",
            "syntax_valid",
            "test_passed",
        ]
        assert sum(weights.values()) == pytest.approx(1.0, abs=1e-6)

    def test_embeds_source_files(self, tmp_path: Path) -> None:
        gt = _sdlc_gt(
            source_files=["django/forms/models.py", "django/forms/widgets.py"]
        )
        script = _build_weighted_checklist_script(
            cmd="pytest tests/",
            repo_path=tmp_path,
            language="python",
            ground_truth=gt,
            header="hdr",
        )
        assert "django/forms/models.py" in script
        assert "django/forms/widgets.py" in script

    def test_embeds_scope_dirs_from_parent_paths(self, tmp_path: Path) -> None:
        gt = _sdlc_gt(
            source_files=["django/forms/models.py", "django/forms/widgets.py"]
        )
        script = _build_weighted_checklist_script(
            cmd="pytest tests/",
            repo_path=tmp_path,
            language="python",
            ground_truth=gt,
            header="hdr",
        )
        # scope_dirs derived from source_file parents
        assert "django/forms" in script

    def test_writes_composite_score_as_last_output(self, tmp_path: Path) -> None:
        """ContinuousScorer's stdout fallback reads the last non-empty line."""
        script = _build_weighted_checklist_script(
            cmd="pytest tests/",
            repo_path=tmp_path,
            language="python",
            ground_truth=_sdlc_gt(),
            header="hdr",
        )
        # Composite score must be printed (ContinuousScorer fallback)
        assert "composite_score" in script or "composite" in script

    def test_cd_uses_task_repo_root_with_fallback(self, tmp_path: Path) -> None:
        script = _build_weighted_checklist_script(
            cmd="pytest tests/",
            repo_path=tmp_path,
            language="python",
            ground_truth=_sdlc_gt(),
            header="hdr",
        )
        # Respects TASK_REPO_ROOT env with mined repo path fallback
        assert "TASK_REPO_ROOT" in script
        assert str(tmp_path) in script

    def test_rejects_invalid_command(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            _build_weighted_checklist_script(
                cmd="pytest tests/; rm -rf /",
                repo_path=tmp_path,
                language="python",
                ground_truth=_sdlc_gt(),
                header="hdr",
            )

    def test_rejects_unknown_prefix(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            _build_weighted_checklist_script(
                cmd="curl evil.example.com",
                repo_path=tmp_path,
                language="python",
                ground_truth=_sdlc_gt(),
                header="hdr",
            )

    def test_non_python_language_still_produces_script(self, tmp_path: Path) -> None:
        # Unknown languages skip syntax check but still produce a valid script
        script = _build_weighted_checklist_script(
            cmd="go test ./...",
            repo_path=tmp_path,
            language="go",
            ground_truth=_sdlc_gt(source_files=["pkg/auth/auth.go"]),
            header="hdr",
        )
        assert "#!/usr/bin/env bash" in script
        assert "go test ./..." in script

    def test_empty_source_files_still_emits_script(self, tmp_path: Path) -> None:
        """Degraded ground truth (no source files) still produces a script."""
        gt = {
            "schema_version": "sdlc-v1",
            "changed_files": [],
            "source_files": [],
            "test_files": [],
            "symbols": [],
            "diff_summary": "",
            "merge_sha": "abc1234567890abc",
            "populated_by": "mining-sdlc-ground-truth",
        }
        script = _build_weighted_checklist_script(
            cmd="pytest tests/",
            repo_path=tmp_path,
            language="python",
            ground_truth=gt,
            header="hdr",
        )
        assert "#!/usr/bin/env bash" in script


# ---------------------------------------------------------------------------
# Tests: write_task_dir integration
# ---------------------------------------------------------------------------


class TestWriteTaskDirUsesWeightedChecklist:
    def test_uses_weighted_when_sdlc_ground_truth_provided(
        self, tmp_path: Path
    ) -> None:
        task = _sdlc_task()
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "repo"
        result_path = write_task_dir(
            task, base_dir, repo_path, ground_truth=_sdlc_gt()
        )
        test_sh = (result_path / "tests" / "test.sh").read_text(encoding="utf-8")
        assert "correct_files" in test_sh
        assert "scope_respected" in test_sh

    def test_falls_back_to_plain_without_ground_truth(self, tmp_path: Path) -> None:
        task = _sdlc_task()
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "repo"
        result_path = write_task_dir(task, base_dir, repo_path)

        test_sh = (result_path / "tests" / "test.sh").read_text(encoding="utf-8")
        assert "correct_files" not in test_sh
        # Plain path still wraps the verification command verbatim
        assert "pytest tests/test_auth.py" in test_sh

    def test_metadata_reward_type_becomes_continuous_for_weighted(
        self, tmp_path: Path
    ) -> None:
        task = _sdlc_task()
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "repo"
        result_path = write_task_dir(
            task, base_dir, repo_path, ground_truth=_sdlc_gt()
        )
        meta = json.loads(
            (result_path / "metadata.json").read_text(encoding="utf-8")
        )
        assert meta["verification"]["reward_type"] == "continuous"

    def test_metadata_reward_type_preserved_without_weighted(
        self, tmp_path: Path
    ) -> None:
        task = _sdlc_task()
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "repo"
        result_path = write_task_dir(task, base_dir, repo_path)

        meta = json.loads(
            (result_path / "metadata.json").read_text(encoding="utf-8")
        )
        assert meta["verification"]["reward_type"] == "binary"

    def test_non_sdlc_schema_falls_back_to_plain(self, tmp_path: Path) -> None:
        """Ground truth with a non-sdlc schema version should not trigger weighted."""
        task = _sdlc_task()
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "repo"
        non_sdlc = {
            "schema_version": "other-v1",
            "changed_files": [],
            "source_files": [],
        }
        result_path = write_task_dir(
            task, base_dir, repo_path, ground_truth=non_sdlc
        )
        test_sh = (result_path / "tests" / "test.sh").read_text(encoding="utf-8")
        assert "correct_files" not in test_sh

    def test_ground_truth_file_still_written_when_weighted(
        self, tmp_path: Path
    ) -> None:
        """ground_truth.json is written alongside the weighted test.sh."""
        task = _sdlc_task()
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "repo"
        gt = _sdlc_gt()
        result_path = write_task_dir(task, base_dir, repo_path, ground_truth=gt)

        gt_path = result_path / "tests" / "ground_truth.json"
        assert gt_path.is_file()
        data = json.loads(gt_path.read_text(encoding="utf-8"))
        assert data["schema_version"] == "sdlc-v1"


# ---------------------------------------------------------------------------
# Integration: generated script executes and emits a valid composite score
# ---------------------------------------------------------------------------


def _init_git_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "t"], check=True
    )


class TestGeneratedScriptEndToEnd:
    def test_full_credit_when_expected_file_modified(self, tmp_path: Path) -> None:
        """End-to-end: agent modifies the expected file → composite >= 0.8."""
        repo = tmp_path / "repo"
        repo.mkdir()
        src_dir = repo / "src"
        src_dir.mkdir()
        source_file = src_dir / "auth.py"
        source_file.write_text("def login():\n    return True\n", encoding="utf-8")
        _init_git_repo(repo)
        subprocess.run(
            ["git", "-C", str(repo), "add", "."], check=True
        )
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True
        )
        # Agent's "fix" — modify the expected file.
        source_file.write_text(
            "def login(user):\n    return user is not None\n", encoding="utf-8"
        )

        gt = _sdlc_gt(source_files=["src/auth.py"])
        script = _build_weighted_checklist_script(
            cmd="bash -c true",
            repo_path=repo,
            language="python",
            ground_truth=gt,
            header="e2e",
        )
        script_path = tmp_path / "tests" / "test.sh"
        script_path.parent.mkdir(parents=True)
        script_path.write_text(script, encoding="utf-8")
        script_path.chmod(0o755)

        result = subprocess.run(
            ["bash", str(script_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Composite score printed as last line: "composite_score=<float>"
        last_line = [
            ln for ln in result.stdout.strip().splitlines() if ln.strip()
        ][-1]
        assert last_line.startswith("composite_score=")
        score = float(last_line.split("=", 1)[1])
        # correct_files=1.0, syntax_valid=1.0, scope_respected=1.0,
        # test_passed=1.0 (cmd 'true') → composite = 1.0
        assert score == pytest.approx(1.0, abs=0.01)

    def test_partial_credit_when_scope_violated(self, tmp_path: Path) -> None:
        """Agent modifies expected file AND an out-of-scope file → scope fails."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "src").mkdir()
        (repo / "src" / "auth.py").write_text("x = 1\n", encoding="utf-8")
        (repo / "docs").mkdir()
        (repo / "docs" / "notes.md").write_text("hi\n", encoding="utf-8")
        _init_git_repo(repo)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True
        )
        # Modify both files (second is outside scope ``src/``).
        (repo / "src" / "auth.py").write_text("x = 2\n", encoding="utf-8")
        (repo / "docs" / "notes.md").write_text("bye\n", encoding="utf-8")

        gt = _sdlc_gt(source_files=["src/auth.py"])
        script = _build_weighted_checklist_script(
            cmd="bash -c true",
            repo_path=repo,
            language="python",
            ground_truth=gt,
            header="e2e",
        )
        script_path = tmp_path / "tests" / "test.sh"
        script_path.parent.mkdir(parents=True)
        script_path.write_text(script, encoding="utf-8")
        script_path.chmod(0o755)

        result = subprocess.run(
            ["bash", str(script_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        last_line = [
            ln for ln in result.stdout.strip().splitlines() if ln.strip()
        ][-1]
        score = float(last_line.split("=", 1)[1])
        # Loses 0.25 for scope violation → composite = 0.75
        assert 0.7 <= score <= 0.8


# ---------------------------------------------------------------------------
# Tests: writable_paths in ground_truth drive scope_dirs (bead codeprobe-br7.5)
# ---------------------------------------------------------------------------


class TestWritablePathsScopeSource:
    def test_prefers_writable_paths_from_ground_truth(self, tmp_path: Path) -> None:
        """When ground_truth carries writable_paths, use them verbatim."""
        gt = _sdlc_gt(source_files=["src/auth.py"])
        gt["writable_paths"] = ["src", "tests"]
        script = _build_weighted_checklist_script(
            cmd="pytest tests/",
            repo_path=tmp_path,
            language="python",
            ground_truth=gt,
            header="hdr",
        )

        match = re.search(r"CP_GROUND_TRUTH='([^']+)'", script)
        assert match is not None
        payload = json.loads(match.group(1))
        assert payload["scope_dirs"] == ["src", "tests"]

    def test_falls_back_to_source_files_parents_when_absent(
        self, tmp_path: Path
    ) -> None:
        """Back-compat: old ground_truth without writable_paths still scopes."""
        gt = _sdlc_gt(source_files=["src/auth.py", "src/session.py"])
        # Explicitly ensure no writable_paths field
        gt.pop("writable_paths", None)
        script = _build_weighted_checklist_script(
            cmd="pytest tests/",
            repo_path=tmp_path,
            language="python",
            ground_truth=gt,
            header="hdr",
        )

        match = re.search(r"CP_GROUND_TRUTH='([^']+)'", script)
        assert match is not None
        payload = json.loads(match.group(1))
        assert payload["scope_dirs"] == ["src"]

    def test_filters_unsafe_writable_paths(self, tmp_path: Path) -> None:
        """Defense-in-depth: unsafe entries are dropped at read time."""
        gt = _sdlc_gt(source_files=["src/auth.py"])
        gt["writable_paths"] = ["../etc", "/absolute", "src"]
        script = _build_weighted_checklist_script(
            cmd="pytest tests/",
            repo_path=tmp_path,
            language="python",
            ground_truth=gt,
            header="hdr",
        )
        match = re.search(r"CP_GROUND_TRUTH='([^']+)'", script)
        assert match is not None
        payload = json.loads(match.group(1))
        assert payload["scope_dirs"] == ["src"]

    def test_normalizes_leading_dot_slash(self, tmp_path: Path) -> None:
        """Writable paths with './' prefix are normalized so the matcher
        stays in sync with the agent-side _normalize()."""
        gt = _sdlc_gt(source_files=["src/auth.py"])
        gt["writable_paths"] = ["./src", "./", "src//tests"]
        script = _build_weighted_checklist_script(
            cmd="pytest tests/",
            repo_path=tmp_path,
            language="python",
            ground_truth=gt,
            header="hdr",
        )
        match = re.search(r"CP_GROUND_TRUTH='([^']+)'", script)
        assert match is not None
        payload = json.loads(match.group(1))
        # './' resolves to '.' which is dropped; './src' → 'src';
        # 'src//tests' → 'src/tests'.
        assert payload["scope_dirs"] == ["src", "src/tests"]

    def test_empty_writable_paths_yields_full_credit(self, tmp_path: Path) -> None:
        """Empty writable_paths → scope check falls through to full credit
        (matches existing semantics when scope_dirs is empty)."""
        gt = _sdlc_gt(source_files=[])
        gt["writable_paths"] = []
        script = _build_weighted_checklist_script(
            cmd="pytest tests/",
            repo_path=tmp_path,
            language="python",
            ground_truth=gt,
            header="hdr",
        )
        match = re.search(r"CP_GROUND_TRUTH='([^']+)'", script)
        assert match is not None
        payload = json.loads(match.group(1))
        assert payload["scope_dirs"] == []


class TestWritablePathsE2E:
    def test_test_file_edit_allowed_when_in_writable_paths(
        self, tmp_path: Path
    ) -> None:
        """Agent editing tests/ passes scope when writable_paths includes it."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "src").mkdir()
        (repo / "src" / "auth.py").write_text("x = 1\n", encoding="utf-8")
        (repo / "tests").mkdir()
        (repo / "tests" / "test_auth.py").write_text(
            "def test_x():\n    pass\n", encoding="utf-8"
        )
        _init_git_repo(repo)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True
        )
        # Agent modifies both the source and the test file.
        (repo / "src" / "auth.py").write_text("x = 2\n", encoding="utf-8")
        (repo / "tests" / "test_auth.py").write_text(
            "def test_x():\n    assert True\n", encoding="utf-8"
        )

        gt = _sdlc_gt(source_files=["src/auth.py"])
        gt["writable_paths"] = ["src", "tests"]
        script = _build_weighted_checklist_script(
            cmd="bash -c true",
            repo_path=repo,
            language="python",
            ground_truth=gt,
            header="e2e",
        )
        script_path = tmp_path / "tests" / "test.sh"
        script_path.parent.mkdir(parents=True)
        script_path.write_text(script, encoding="utf-8")
        script_path.chmod(0o755)

        result = subprocess.run(
            ["bash", str(script_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        last_line = [
            ln for ln in result.stdout.strip().splitlines() if ln.strip()
        ][-1]
        score = float(last_line.split("=", 1)[1])
        # correct_files=1.0, syntax=1.0, scope=1.0 (both dirs covered),
        # test=1.0 → composite = 1.0
        assert score == pytest.approx(1.0, abs=0.01)
