"""Runtime tests for the vendored oracle.py template.

These exercise the actual rendered template by writing it to a temp dir and
running it as a subprocess against synthetic ground_truth + answer files.
The contract under test: oracle.py writes ``metrics.json`` containing
precision/recall/f1/matched/expected_count/agent_files_count alongside
``reward.txt``. ContinuousScorer relies on this contract to surface
breakdowns in ``ScoreResult.details`` without changing the headline score.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from codeprobe.mining.writer import _ORACLE_PY


def _setup_task(
    tmp_path: Path,
    *,
    expected: list[str],
    answer_lines: list[str],
    repo: str = "",
    oracle_tiers: dict[str, str] | None = None,
) -> Path:
    """Materialize a task dir with oracle.py, ground_truth.json, and answer.txt."""
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "oracle.py").write_text(_ORACLE_PY)
    gt: dict[str, object] = {"expected": expected, "repo": repo}
    if oracle_tiers is not None:
        gt["oracle_tiers"] = oracle_tiers
    (task_dir / "ground_truth.json").write_text(json.dumps(gt))
    (task_dir / "answer.txt").write_text("\n".join(answer_lines) + "\n")
    return task_dir


def _run_oracle(task_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(task_dir / "oracle.py"), str(task_dir)],
        capture_output=True,
        text=True,
        check=False,
    )


class TestOracleMetricsJson:
    """Oracle template emits metrics.json with the documented schema."""

    def test_writes_metrics_json_with_precision_recall_f1(
        self, tmp_path: Path
    ) -> None:
        """Happy path: 3-of-4 expected, 1 false positive — non-trivial P and R."""
        task_dir = _setup_task(
            tmp_path,
            expected=["pkg/a.go", "pkg/b.go", "pkg/c.go", "pkg/d.go"],
            answer_lines=["pkg/a.go", "pkg/b.go", "pkg/c.go", "pkg/extra.go"],
        )
        result = _run_oracle(task_dir)
        assert result.returncode == 0, result.stderr

        metrics = json.loads((task_dir / "metrics.json").read_text())
        assert metrics["metric"] == "f1"
        assert metrics["matched"] == 3
        assert metrics["expected_count"] == 4
        assert metrics["agent_files_count"] == 4
        assert metrics["precision"] == pytest.approx(0.75)
        assert metrics["recall"] == pytest.approx(0.75)
        assert metrics["f1"] == pytest.approx(0.75)
        assert metrics["score"] == pytest.approx(0.75)
        assert metrics["weighted_recall"] is None

        reward = (task_dir / "reward.txt").read_text().strip()
        assert reward == "0.7500"

    def test_brute_force_high_recall_low_precision_visible_in_metrics(
        self, tmp_path: Path
    ) -> None:
        """Regression: the gascity baseline shape (R=1.0, P=0.26) must be
        visible in metrics.json so an aggregate report can distinguish it
        from a balanced answer at the same F1."""
        expected = [f"pkg/a{i}.go" for i in range(80)]
        # Brute-force "ship everything that contains the bytes": 80 hits + 231
        # false positives. Mirrors the real run we observed.
        answer = expected + [f"pkg/junk{i}.go" for i in range(231)]
        task_dir = _setup_task(tmp_path, expected=expected, answer_lines=answer)
        result = _run_oracle(task_dir)
        assert result.returncode == 0, result.stderr

        metrics = json.loads((task_dir / "metrics.json").read_text())
        assert metrics["recall"] == pytest.approx(1.0)
        assert metrics["precision"] == pytest.approx(80 / 311, abs=1e-6)
        assert metrics["f1"] == pytest.approx(0.4092, abs=1e-3)
        assert metrics["matched"] == 80
        assert metrics["agent_files_count"] == 311
        assert metrics["expected_count"] == 80

    def test_metrics_json_on_missing_answer(self, tmp_path: Path) -> None:
        """No answer.txt — metrics.json still written with zeros + error tag."""
        task_dir = _setup_task(
            tmp_path,
            expected=["pkg/a.go"],
            answer_lines=[],
        )
        # Remove the answer.txt that _setup_task created
        (task_dir / "answer.txt").unlink()

        result = _run_oracle(task_dir)
        assert result.returncode == 0  # exits 0 with score 0
        metrics = json.loads((task_dir / "metrics.json").read_text())
        assert metrics["score"] == 0.0
        assert metrics["matched"] == 0
        assert metrics["expected_count"] == 1
        assert metrics["agent_files_count"] == 0
        assert metrics["error"] == "no_answer_file"

    def test_metrics_json_on_empty_answer(self, tmp_path: Path) -> None:
        """answer.txt exists but contains no usable lines — still written."""
        task_dir = _setup_task(
            tmp_path,
            expected=["pkg/a.go", "pkg/b.go"],
            answer_lines=["", "# only a comment"],
        )
        result = _run_oracle(task_dir)
        assert result.returncode == 0
        metrics = json.loads((task_dir / "metrics.json").read_text())
        assert metrics["score"] == 0.0
        assert metrics["agent_files_count"] == 0
        assert metrics["error"] == "empty_answer"

    def test_metrics_json_with_oracle_tiers(self, tmp_path: Path) -> None:
        """Tiered ground truth → metric=weighted_f1 and weighted_recall is set."""
        task_dir = _setup_task(
            tmp_path,
            expected=["pkg/a.go", "pkg/b.go", "pkg/c.go"],
            answer_lines=["pkg/a.go", "pkg/b.go"],
            oracle_tiers={
                "pkg/a.go": "required",
                "pkg/b.go": "supplementary",
                "pkg/c.go": "context",
            },
        )
        result = _run_oracle(task_dir)
        assert result.returncode == 0, result.stderr
        metrics = json.loads((task_dir / "metrics.json").read_text())
        assert metrics["metric"] == "weighted_f1"
        assert metrics["weighted_recall"] is not None
        # Required (2.0) + Supplementary (1.0) of total 2+1+0.5 = 3.5
        assert metrics["weighted_recall"] == pytest.approx(3.0 / 3.5)
