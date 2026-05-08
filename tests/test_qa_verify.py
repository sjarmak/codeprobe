"""Tests for the codeprobe-side benchmark_qa_core adapter (qa/verify.py).

Covers:
* TOML and JSON metadata parsing into a flat dict that exposes the keys
  benchmark_qa_core's ``check_scoring_honesty`` looks at.
* End-to-end ``verify_task_qa`` orchestration: oracle_files / symbols /
  tokens projection from each ground_truth.json schema flavour.
* The Class-D suppression default (codeprobe scoring already weights
  off-scope answers, so D1/D2 findings are noise unless opted in).
* Aux-file leakage detection on a synthetic context file containing the
  oracle answer verbatim.
* Codeprobe ``reward_type`` → benchmark_qa_core ``scoring_method``
  projection so existing tasks don't all flag E1.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from codeprobe.qa.verify import (
    DEFAULT_SCORING_METHOD_TIERS,
    QAVerifyResult,
    load_task_meta,
    verify_task_qa,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_task(
    base: Path,
    *,
    task_id: str = "t",
    task_toml: str | None = None,
    metadata_json: dict | None = None,
    instruction: str = "Do the thing.\n",
    ground_truth: dict | None = None,
    oracle_files_to_create: list[str] | None = None,
) -> Path:
    """Create a minimal task dir and return its path."""
    task_dir = base / task_id
    (task_dir / "tests").mkdir(parents=True, exist_ok=True)
    if task_toml is not None:
        (task_dir / "task.toml").write_text(task_toml, encoding="utf-8")
    if metadata_json is not None:
        (task_dir / "metadata.json").write_text(
            json.dumps(metadata_json), encoding="utf-8"
        )
    (task_dir / "instruction.md").write_text(instruction, encoding="utf-8")
    if ground_truth is not None:
        (task_dir / "tests" / "ground_truth.json").write_text(
            json.dumps(ground_truth), encoding="utf-8"
        )
    if oracle_files_to_create:
        for rel in oracle_files_to_create:
            target = task_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# stub\n", encoding="utf-8")
    return task_dir


# ---------------------------------------------------------------------------
# load_task_meta
# ---------------------------------------------------------------------------


class TestLoadTaskMeta:
    def test_parses_task_toml_flat(self, tmp_path: Path) -> None:
        toml = textwrap.dedent(
            """
            [task]
            id = "t1"

            [metadata]
            language = "python"
            difficulty = "easy"

            [verification]
            reward_type = "binary"
            """
        )
        task_dir = _write_task(tmp_path, task_toml=toml)
        meta = load_task_meta(task_dir)
        # Flattened: keys from each section live at the top.
        assert meta["id"] == "t1"
        assert meta["language"] == "python"
        assert meta["reward_type"] == "binary"

    def test_parses_metadata_json_flat(self, tmp_path: Path) -> None:
        # metadata.json sections (verification, metadata, task) are
        # flattened the same way TOML sections are, so QA-lib callers
        # don't need to know which on-disk format the task uses.
        task_dir = _write_task(
            tmp_path,
            metadata_json={
                "id": "t1",
                "verification": {"reward_type": "binary"},
                "metadata": {"language": "python"},
            },
        )
        meta = load_task_meta(task_dir)
        assert meta["id"] == "t1"
        assert meta["reward_type"] == "binary"
        assert meta["language"] == "python"

    def test_returns_empty_when_no_metadata(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "empty"
        task_dir.mkdir()
        assert load_task_meta(task_dir) == {}


# ---------------------------------------------------------------------------
# verify_task_qa — happy path
# ---------------------------------------------------------------------------


class TestVerifyTaskQAHappyPath:
    def test_valid_dual_task_returns_no_findings(self, tmp_path: Path) -> None:
        toml = textwrap.dedent(
            """
            [task]
            id = "ok"
            [metadata]
            language = "python"
            [verification]
            reward_type = "binary"
            """
        )
        task_dir = _write_task(
            tmp_path,
            task_id="ok",
            task_toml=toml,
            instruction="A non-leaking instruction.\n",
            ground_truth={"answer_type": "text", "answer": "answer-of-the-task"},
        )
        result = verify_task_qa(task_dir)
        # Info-severity findings are advisory and do not flip ok.
        assert result.ok
        non_info = [f for f in result.findings if f.severity != "info"]
        assert non_info == []

    def test_missing_task_dir_returns_a1(self, tmp_path: Path) -> None:
        result = verify_task_qa(tmp_path / "nope")
        assert not result.ok
        assert any(f.code == "A1" for f in result.findings)


# ---------------------------------------------------------------------------
# Oracle file checks (A1) — uses real on-disk files
# ---------------------------------------------------------------------------


class TestOracleFileChecks:
    def test_missing_file_flags_a1(self, tmp_path: Path) -> None:
        toml = '[task]\nid = "t"\n[verification]\nreward_type = "binary"\n'
        task_dir = _write_task(
            tmp_path,
            task_toml=toml,
            ground_truth={
                "answer_type": "file_list",
                "answer": ["src/missing.py"],
            },
        )
        # repo_root defaults to task_dir; src/missing.py does not exist.
        result = verify_task_qa(task_dir)
        assert any(f.code == "A1" for f in result.findings)

    def test_existing_file_passes(self, tmp_path: Path) -> None:
        toml = textwrap.dedent(
            """
            [task]
            id = "t"
            [metadata]
            language = "python"
            [verification]
            reward_type = "binary"
            """
        )
        task_dir = _write_task(
            tmp_path,
            task_toml=toml,
            ground_truth={
                "answer_type": "file_list",
                "answer": ["src/real.py"],
            },
            oracle_files_to_create=["src/real.py"],
        )
        result = verify_task_qa(task_dir)
        assert result.ok

    def test_v2_checks_format_extracts_files(self, tmp_path: Path) -> None:
        toml = textwrap.dedent(
            """
            [task]
            id = "t"
            [verification]
            reward_type = "binary"
            """
        )
        task_dir = _write_task(
            tmp_path,
            task_toml=toml,
            ground_truth={
                "checks": [
                    {
                        "answer_type": "file_list",
                        "answer": ["a.py"],
                        "weight": 0.5,
                    },
                    {
                        "answer_type": "count",
                        "answer": 1,
                        "weight": 0.5,
                    },
                ],
            },
        )
        result = verify_task_qa(task_dir)
        # a.py does not exist → A1 finding from the file-list extraction.
        assert any(f.code == "A1" and "a.py" in f.message for f in result.findings)


# ---------------------------------------------------------------------------
# Class-D suppression
# ---------------------------------------------------------------------------


class TestClassDSuppression:
    def test_d_findings_suppressed_by_default(self, tmp_path: Path) -> None:
        # Show that the lib WOULD produce D-findings for an in-scope/
        # out-of-scope mismatch, then show that the orchestrator strips
        # them from its output. (verify_task_qa doesn't currently surface
        # path constraints — codeprobe's scoring layer covers that — so
        # we drive the lib directly to confirm the suppression default.)
        from codeprobe.qa.benchmark_qa_core import (
            OracleConstraints,
            check_oracle_coherence,
        )

        # Create a file outside the include scope so D1 fires.
        (tmp_path / "other").mkdir()
        (tmp_path / "other" / "file.py").write_text("# stub\n")
        constraints = OracleConstraints(path_include=("src/*",))
        findings = check_oracle_coherence(
            instruction_text="",
            oracle_files=["other/file.py"],
            oracle_symbols=[],
            repo_root=tmp_path,
            constraints=constraints,
        )
        # D1 is the bare-lib behaviour we're suppressing.
        assert any(f.code == "D1" for f in findings)


# ---------------------------------------------------------------------------
# Scoring honesty (E*)
# ---------------------------------------------------------------------------


class TestScoringHonesty:
    def test_known_reward_type_passes(self, tmp_path: Path) -> None:
        toml = textwrap.dedent(
            """
            [task]
            id = "t"
            [verification]
            reward_type = "binary"
            """
        )
        task_dir = _write_task(
            tmp_path,
            task_toml=toml,
            ground_truth={"answer_type": "count", "answer": 1},
        )
        result = verify_task_qa(task_dir)
        # binary maps to "strict" tier — no E findings.
        assert not any(f.code in {"E1", "E2", "E3"} for f in result.findings)

    def test_unknown_reward_type_flags_e2(self, tmp_path: Path) -> None:
        toml = textwrap.dedent(
            """
            [task]
            id = "t"
            [verification]
            reward_type = "homemade_metric"
            """
        )
        task_dir = _write_task(
            tmp_path,
            task_toml=toml,
            ground_truth={"answer_type": "count", "answer": 1},
        )
        result = verify_task_qa(task_dir)
        assert any(f.code == "E2" for f in result.findings)

    def test_no_reward_type_flags_e1(self, tmp_path: Path) -> None:
        toml = textwrap.dedent(
            """
            [task]
            id = "t"
            [verification]
            type = "test_script"
            """
        )
        task_dir = _write_task(
            tmp_path,
            task_toml=toml,
            ground_truth={"answer_type": "count", "answer": 1},
        )
        result = verify_task_qa(task_dir)
        assert any(f.code == "E1" for f in result.findings)


# ---------------------------------------------------------------------------
# Aux-file leakage (F*)
# ---------------------------------------------------------------------------


class TestAuxFileLeakage:
    def test_oracle_token_in_instruction_flags_f2(self, tmp_path: Path) -> None:
        # Oracle expects file paths "src/secret_helper.py".
        # Instruction (instruction.md is auto-included as aux) leaks the path.
        toml = textwrap.dedent(
            """
            [task]
            id = "leaky"
            [verification]
            reward_type = "binary"
            """
        )
        task_dir = _write_task(
            tmp_path,
            task_id="leaky",
            task_toml=toml,
            instruction="Touch the secret_helper file.\n",
            ground_truth={
                "answer_type": "file_list",
                "answer": ["secret_helper"],
            },
            oracle_files_to_create=["secret_helper"],
        )
        result = verify_task_qa(task_dir)
        f2 = [f for f in result.findings if f.code == "F2"]
        assert f2, f"Expected F2 leakage finding, got: {result.findings}"

    def test_token_below_threshold_no_f2(self, tmp_path: Path) -> None:
        # 2-char tokens are info-only (F1), never error.
        toml = '[task]\nid = "x"\n[verification]\nreward_type = "binary"\n'
        task_dir = _write_task(
            tmp_path,
            task_toml=toml,
            instruction="Look at xx.\n",
            ground_truth={"answer_type": "text", "answer": "xx"},
        )
        result = verify_task_qa(task_dir)
        # F2 is error-severity; F1 is info — make sure no F2 was emitted.
        assert not any(f.code == "F2" for f in result.findings)


# ---------------------------------------------------------------------------
# Default tier table
# ---------------------------------------------------------------------------


class TestDefaultTierTable:
    def test_codeprobe_reward_types_in_table(self) -> None:
        # All codeprobe reward_type values that ship out of the box must
        # map to a sanctioned tier.
        for reward_type in (
            "binary",
            "exact_match",
            "continuous",
            "file_list",
            "symbol_list",
            "dual",
        ):
            assert reward_type in DEFAULT_SCORING_METHOD_TIERS
            tier = DEFAULT_SCORING_METHOD_TIERS[reward_type]
            assert tier and tier.lower() != "unknown"


# ---------------------------------------------------------------------------
# QAVerifyResult flags
# ---------------------------------------------------------------------------


class TestRepoRootAutoDetect:
    """When ``repo_root`` is None, mined tasks (metadata carries ``repo`` /
    ``repo_path``) must resolve oracle paths against the nearest ancestor
    project root (pyproject.toml or .git), not against task_dir.
    Synthetic tasks (no repo hint) keep the legacy task_dir default.
    """

    def test_mined_fixture_resolves_against_repo_root(self) -> None:
        # The committed fixture under tests/fixtures/qa_mined_task references
        # src/codeprobe/qa/verify.py, which exists at the repo root. Without
        # auto-detect this would A1; with it, the call should be clean.
        fixture_dir = Path(__file__).parent / "fixtures" / "qa_mined_task"
        result = verify_task_qa(fixture_dir)
        a1 = [f for f in result.findings if f.code == "A1"]
        assert a1 == [], f"Expected zero A1, got: {a1}"
        assert result.ok

    def test_synthetic_task_keeps_task_dir_default(self, tmp_path: Path) -> None:
        # No repo / repo_path → fall back to task_dir. The oracle file
        # exists *inside* the task bundle, which is the synthetic-task
        # contract. With the old default (task_dir-relative) this passes;
        # with auto-detect it must also pass and not climb up to a
        # surrounding pyproject.toml.
        toml = textwrap.dedent(
            """
            [task]
            id = "synthetic"
            [verification]
            reward_type = "binary"
            """
        )
        task_dir = _write_task(
            tmp_path,
            task_id="synthetic",
            task_toml=toml,
            ground_truth={
                "answer_type": "file_list",
                "answer": ["bundled.py"],
            },
            oracle_files_to_create=["bundled.py"],
        )
        result = verify_task_qa(task_dir)
        a1 = [f for f in result.findings if f.code == "A1"]
        assert a1 == [], f"Expected zero A1, got: {a1}"

    def test_repo_path_in_metadata_resolves_to_subdir(self, tmp_path: Path) -> None:
        # repo_path names a relative subdir of the project root; auto-detect
        # should descend into it to resolve oracles.
        # Layout:
        #   tmp_path/pyproject.toml  (boundary marker)
        #   tmp_path/sub/oracle.py   (the actual oracle file)
        #   tmp_path/tasks/t/        (the task dir, with repo_path = "sub")
        (tmp_path / "pyproject.toml").write_text("# stub\n")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "oracle.py").write_text("# oracle\n")
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        toml = textwrap.dedent(
            """
            [task]
            id = "t"
            repo_path = "sub"
            [verification]
            reward_type = "binary"
            """
        )
        task_dir = _write_task(
            tasks_dir,
            task_id="t",
            task_toml=toml,
            ground_truth={
                "answer_type": "file_list",
                "answer": ["oracle.py"],
            },
        )
        result = verify_task_qa(task_dir)
        a1 = [f for f in result.findings if f.code == "A1"]
        assert a1 == [], f"Expected zero A1, got: {a1}"


class TestQAVerifyResultFlags:
    def test_ok_true_when_only_warnings(self) -> None:
        from codeprobe.qa.benchmark_qa_core import Finding

        result = QAVerifyResult(
            task_dir=Path("."),
            findings=[Finding(severity="warning", code="X1", message="m")],
        )
        assert result.ok
        assert result.warning_count == 1
        assert result.error_count == 0

    def test_ok_false_when_any_error(self) -> None:
        from codeprobe.qa.benchmark_qa_core import Finding

        result = QAVerifyResult(
            task_dir=Path("."),
            findings=[Finding(severity="error", code="X1", message="m")],
        )
        assert not result.ok
        assert result.error_count == 1
