"""Tests for codeprobe.analysis.interpret.regression_report (R20)."""

from __future__ import annotations

import json
from pathlib import Path

from codeprobe.analysis.interpret import (
    TaskRegression,
    collect_task_regressions,
    format_regression_report,
    regression_report,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_task(
    tasks_dir: Path,
    task_id: str,
    history: tuple[str, ...],
    commit: str = "",
) -> Path:
    """Write a minimal metadata.json for a task under ``tasks_dir``."""
    td = tasks_dir / task_id
    td.mkdir(parents=True, exist_ok=True)
    (td / "instruction.md").write_text("stub\n", encoding="utf-8")
    meta = {
        "id": task_id,
        "repo": "example/repo",
        "metadata": {
            "name": task_id,
            "ground_truth_commit": commit or (history[-1] if history else ""),
            "ground_truth_commit_history": list(history),
        },
        "verification": {},
    }
    (td / "metadata.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    return td


# ---------------------------------------------------------------------------
# collect_task_regressions
# ---------------------------------------------------------------------------


class TestCollect:
    def test_empty_dir_returns_empty_list(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        assert collect_task_regressions(tasks_dir) == []

    def test_missing_dir_returns_empty_list(self, tmp_path: Path) -> None:
        assert collect_task_regressions(tmp_path / "does-not-exist") == []

    def test_groups_by_task_id(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        _write_task(tasks_dir, "tsk-a", ("c1", "c2"))
        _write_task(tasks_dir, "tsk-b", ("d1", "d2", "d3"))

        regs = collect_task_regressions(tasks_dir)
        ids = [r.task_id for r in regs]
        # Sorted by task_id.
        assert ids == ["tsk-a", "tsk-b"]
        assert regs[0].commits == ("c1", "c2")
        assert regs[1].commits == ("d1", "d2", "d3")

    def test_commits_oldest_to_newest(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        _write_task(tasks_dir, "tsk-ord", ("first", "second", "third"))
        regs = collect_task_regressions(tasks_dir)
        assert regs[0].commits == ("first", "second", "third")

    def test_falls_back_to_single_commit_field(self, tmp_path: Path) -> None:
        """Tasks mined before R20 have no history — use ground_truth_commit."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        _write_task(tasks_dir, "tsk-legacy", history=(), commit="legacy-sha")
        regs = collect_task_regressions(tasks_dir)
        assert regs[0].commits == ("legacy-sha",)

    def test_task_without_any_commit_has_empty_tuple(
        self, tmp_path: Path
    ) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        _write_task(tasks_dir, "tsk-bare", history=(), commit="")
        regs = collect_task_regressions(tasks_dir)
        assert regs[0].commits == ()
        assert regs[0].scores == ()

    def test_duplicate_task_id_deduped(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        # Force two dirs with colliding IDs by writing to different dirs.
        d1 = tasks_dir / "one"
        d2 = tasks_dir / "two"
        for d in (d1, d2):
            d.mkdir()
            (d / "metadata.json").write_text(
                json.dumps(
                    {
                        "id": "same-id",
                        "metadata": {
                            "ground_truth_commit_history": ["c1", "c2"],
                        },
                    }
                )
            )
        regs = collect_task_regressions(tasks_dir)
        assert len(regs) == 1

    def test_scores_per_commit_layout(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        _write_task(tasks_dir, "tsk-s", ("c1", "c2"))

        results_dir = tmp_path / "results"
        (results_dir / "c1").mkdir(parents=True)
        (results_dir / "c2").mkdir(parents=True)
        (results_dir / "c1" / "scores.json").write_text(
            json.dumps({"tsk-s": 0.5})
        )
        (results_dir / "c2" / "scores.json").write_text(
            json.dumps({"tsk-s": 0.9})
        )

        regs = collect_task_regressions(tasks_dir, results_dir=results_dir)
        assert regs[0].scores == (0.5, 0.9)

    def test_scores_missing_left_as_none(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        _write_task(tasks_dir, "tsk-m", ("c1", "c2"))

        regs = collect_task_regressions(tasks_dir, results_dir=tmp_path / "r")
        assert regs[0].scores == (None, None)


# ---------------------------------------------------------------------------
# format_regression_report
# ---------------------------------------------------------------------------


class TestFormat:
    def test_empty_report(self) -> None:
        text = format_regression_report([])
        assert "Task ID" in text
        assert "no tasks" in text.lower()

    def test_includes_every_task_id(self) -> None:
        regs = [
            TaskRegression(
                task_id="tsk-a", commits=("c1", "c2"), scores=(0.5, 0.9)
            ),
            TaskRegression(
                task_id="tsk-b", commits=("d1",), scores=(None,)
            ),
        ]
        text = format_regression_report(regs)
        assert "tsk-a" in text
        assert "tsk-b" in text

    def test_scores_rendered(self) -> None:
        regs = [
            TaskRegression(
                task_id="tsk-s",
                commits=("abcdef0123", "1234567890"),
                scores=(0.25, 0.75),
            ),
        ]
        text = format_regression_report(regs)
        assert "0.25" in text
        assert "0.75" in text

    def test_missing_score_renders_em_dash(self) -> None:
        regs = [
            TaskRegression(task_id="t", commits=("c1",), scores=(None,)),
        ]
        text = format_regression_report(regs)
        assert "—" in text

    def test_commits_in_order(self) -> None:
        regs = [
            TaskRegression(
                task_id="t",
                commits=("first0", "second", "third0"),
                scores=(None, None, None),
            ),
        ]
        text = format_regression_report(regs)
        # Just confirm older appears before newer in the rendered output.
        idx_first = text.find("first")
        idx_second = text.find("second")
        idx_third = text.find("third")
        assert 0 <= idx_first < idx_second < idx_third


# ---------------------------------------------------------------------------
# regression_report (end-to-end)
# ---------------------------------------------------------------------------


class TestRegressionReportE2E:
    def test_end_to_end(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        _write_task(tasks_dir, "tsk-e2e", ("aaaaaaa", "bbbbbbb"))

        results_dir = tmp_path / "results"
        (results_dir / "aaaaaaa").mkdir(parents=True)
        (results_dir / "bbbbbbb").mkdir(parents=True)
        (results_dir / "aaaaaaa" / "scores.json").write_text(
            json.dumps({"tsk-e2e": 0.2})
        )
        (results_dir / "bbbbbbb" / "scores.json").write_text(
            json.dumps({"tsk-e2e": 0.8})
        )

        text = regression_report(tasks_dir, results_dir=results_dir)
        assert "tsk-e2e" in text
        assert "0.20" in text
        assert "0.80" in text

    def test_end_to_end_no_results(self, tmp_path: Path) -> None:
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        _write_task(tasks_dir, "tsk-norsults", ("c1",))

        text = regression_report(tasks_dir, results_dir=None)
        assert "tsk-norsults" in text
