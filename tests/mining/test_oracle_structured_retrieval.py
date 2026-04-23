"""R2: oracle_type='structured_retrieval' oracle and writer paths."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from codeprobe.mining.writer import write_task_dir
from codeprobe.models.task import Task, TaskMetadata, TaskVerification

# A structured ground-truth payload used across tests.
_EXPECTED = {
    "files": [
        {"repo": "myrepo", "path": "pkg/a.py"},
        {"repo": "myrepo", "path": "pkg/b.py"},
    ],
    "symbols": [
        {"repo": "myrepo", "path": "pkg/a.py", "symbol": "alpha"},
        {"repo": "myrepo", "path": "pkg/b.py", "symbol": "beta"},
    ],
    "chain": [
        {"repo": "myrepo", "path": "pkg/a.py", "symbol": "alpha"},
        {"repo": "myrepo", "path": "pkg/b.py", "symbol": "beta"},
    ],
    "text": "Alpha calls beta via the shared scheduler.",
}


def _make_structured_task() -> Task:
    return Task(
        id="struct001",
        repo="myrepo",
        metadata=TaskMetadata(
            name="merge-struct001",
            difficulty="hard",
            description="Structured retrieval task",
            language="python",
            task_type="org_scale_cross_repo",
            org_scale=True,
            issue_title="Find the call chain from alpha to beta",
            issue_body="Trace the call chain.",
            category="symbol-reference-trace",
        ),
        verification=TaskVerification(
            type="oracle",
            command="bash tests/test.sh",
            reward_type="continuous",
            oracle_type="structured_retrieval",
            # oracle_answer is tuple[str, ...] — store the structured
            # expected payload as a single JSON string; writer decodes it.
            oracle_answer=(json.dumps(_EXPECTED),),
        ),
    )


def _write_task(tmp_path: Path) -> Path:
    """Write a structured-retrieval task and return the task directory."""
    task = _make_structured_task()
    return write_task_dir(task, tmp_path / "tasks", tmp_path / "myrepo")


def _run_oracle(task_dir: Path) -> dict:
    """Run the vendored oracle.py and return scoring.json."""
    oracle_py = task_dir / "tests" / "oracle.py"
    assert oracle_py.exists(), "oracle.py missing"
    proc = subprocess.run(
        [sys.executable, str(oracle_py), str(task_dir)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    # Oracle always exits 0; verdict lives in scoring.json.
    assert proc.returncode == 0, f"oracle stderr: {proc.stderr}"
    scoring_path = task_dir / "scoring.json"
    assert scoring_path.exists(), "scoring.json missing"
    return json.loads(scoring_path.read_text())


# ---------------------------------------------------------------------------
# Valid JSON — per-field F1 scored independently
# ---------------------------------------------------------------------------


class TestValidStructuredAnswer:
    def test_perfect_match_scores_one(self, tmp_path: Path) -> None:
        task_dir = _write_task(tmp_path)
        (task_dir / "answer.json").write_text(json.dumps(_EXPECTED))
        scoring = _run_oracle(task_dir)
        assert scoring["score"] == 1.0
        assert scoring.get("error") is None
        assert scoring["fields"]["files"]["score"] == 1.0
        assert scoring["fields"]["symbols"]["score"] == 1.0
        assert scoring["fields"]["chain"]["score"] == 1.0
        assert scoring["fields"]["text"]["score"] == 1.0

    def test_per_field_independence(self, tmp_path: Path) -> None:
        """Each field is scored on its own before averaging."""
        task_dir = _write_task(tmp_path)
        answer = {
            # files: full match
            "files": _EXPECTED["files"],
            # symbols: wrong — no matches
            "symbols": [
                {"repo": "myrepo", "path": "pkg/a.py", "symbol": "wrong"},
            ],
            # chain: full match
            "chain": _EXPECTED["chain"],
            # text: unrelated — no token overlap
            "text": "xyz qux quux",
        }
        (task_dir / "answer.json").write_text(json.dumps(answer))
        scoring = _run_oracle(task_dir)
        assert scoring["fields"]["files"]["score"] == 1.0
        assert scoring["fields"]["symbols"]["score"] == 0.0
        assert scoring["fields"]["chain"]["score"] == 1.0
        assert 0.0 <= scoring["fields"]["text"]["score"] < 1.0

        # Combined = mean of the 4 per-field scores
        per_field = [
            scoring["fields"]["files"]["score"],
            scoring["fields"]["symbols"]["score"],
            scoring["fields"]["chain"]["score"],
            scoring["fields"]["text"]["score"],
        ]
        expected_combined = sum(per_field) / 4
        assert abs(scoring["score"] - expected_combined) < 1e-6


# ---------------------------------------------------------------------------
# Missing / malformed — 0.0 with explicit error (INV1: no silent zero)
# ---------------------------------------------------------------------------


class TestMalformedAnswer:
    def test_missing_answer_json(self, tmp_path: Path) -> None:
        task_dir = _write_task(tmp_path)
        # Do NOT create answer.json
        scoring = _run_oracle(task_dir)
        assert scoring["score"] == 0.0
        assert "missing" in (scoring.get("error") or "")

    def test_malformed_answer_json(self, tmp_path: Path) -> None:
        task_dir = _write_task(tmp_path)
        (task_dir / "answer.json").write_text("{not json at all")
        scoring = _run_oracle(task_dir)
        assert scoring["score"] == 0.0
        assert "malformed" in (scoring.get("error") or "").lower()

    def test_non_object_answer_json(self, tmp_path: Path) -> None:
        task_dir = _write_task(tmp_path)
        (task_dir / "answer.json").write_text(json.dumps([1, 2, 3]))
        scoring = _run_oracle(task_dir)
        assert scoring["score"] == 0.0
        assert scoring.get("error") is not None


# ---------------------------------------------------------------------------
# Partial match — non-zero, less than one
# ---------------------------------------------------------------------------


class TestPartialMatch:
    def test_partial_files_only(self, tmp_path: Path) -> None:
        task_dir = _write_task(tmp_path)
        answer = {
            # only one of two files
            "files": [{"repo": "myrepo", "path": "pkg/a.py"}],
            "symbols": [],
            "chain": [],
            "text": "",
        }
        (task_dir / "answer.json").write_text(json.dumps(answer))
        scoring = _run_oracle(task_dir)
        # files: P=1.0, R=0.5, F1≈0.666 ; symbols/chain: P=R=0, score=0.0 ;
        # text: P=0 (empty answer), so score=0.0
        assert 0.0 < scoring["score"] < 1.0
        assert scoring["fields"]["files"]["score"] > 0.0
        assert scoring["fields"]["files"]["score"] < 1.0


# ---------------------------------------------------------------------------
# INV1 — NO $AGENT_OUTPUT fallback anywhere in the structured oracle region
# ---------------------------------------------------------------------------


class TestNoAgentOutputFallback:
    def test_oracle_script_has_no_agent_output(self, tmp_path: Path) -> None:
        task_dir = _write_task(tmp_path)
        oracle_src = (task_dir / "tests" / "oracle.py").read_text()
        assert "AGENT_OUTPUT" not in oracle_src

    def test_test_sh_has_no_agent_output(self, tmp_path: Path) -> None:
        task_dir = _write_task(tmp_path)
        test_sh = (task_dir / "tests" / "test.sh").read_text()
        assert "AGENT_OUTPUT" not in test_sh


# ---------------------------------------------------------------------------
# instruction.md ships the answer.json schema section
# ---------------------------------------------------------------------------


class TestInstructionSchemaSection:
    def test_instruction_contains_schema_fields(self, tmp_path: Path) -> None:
        task_dir = _write_task(tmp_path)
        content = (task_dir / "instruction.md").read_text()
        assert "answer.json" in content
        for field in ("files", "symbols", "chain", "text"):
            assert f"`{field}`" in content, f"missing `{field}` in schema section"

    def test_ground_truth_json_records_structured_schema(self, tmp_path: Path) -> None:
        task_dir = _write_task(tmp_path)
        gt = json.loads((task_dir / "ground_truth.json").read_text())
        assert gt["oracle_type"] == "structured_retrieval"
        expected = gt["expected"]
        assert isinstance(expected, dict)
        for field in ("files", "symbols", "chain", "text"):
            assert field in expected


# ---------------------------------------------------------------------------
# Legacy answer_type='file_list' path preserved
# ---------------------------------------------------------------------------


class TestFileListLegacyPathPreserved:
    def test_file_list_oracle_unchanged(self, tmp_path: Path) -> None:
        """answer_type='file_list' still uses the F1 oracle (R2 requirement)."""
        task = Task(
            id="flist001",
            repo="myrepo",
            metadata=TaskMetadata(
                name="merge-flist001",
                difficulty="medium",
                description="File list task",
                language="python",
                task_type="org_scale_cross_repo",
                org_scale=True,
                issue_title="Find matches",
                issue_body="Find the patterns.",
                category="symbol-reference-trace",
            ),
            verification=TaskVerification(
                type="oracle",
                command="bash tests/test.sh",
                reward_type="continuous",
                oracle_type="file_list",
                oracle_answer=("pkg/a.py", "pkg/b.py"),
            ),
        )
        task_dir = write_task_dir(task, tmp_path / "tasks", tmp_path / "myrepo")

        oracle_src = (task_dir / "tests" / "oracle.py").read_text()
        # file_list oracle works off answer.txt, not answer.json
        assert "answer.txt" in oracle_src
        # And the legacy path IS allowed to reference AGENT_OUTPUT.
        test_sh = (task_dir / "tests" / "test.sh").read_text()
        assert "AGENT_OUTPUT" in test_sh
