"""Tests for the self-contained HTML run-data explorer (codeprobe-3cs)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeprobe.analysis.run_explorer import (
    EXPLORER_FILE,
    build_arm_summaries,
    compute_validity_flags,
    load_run_trials,
    newest_run_dir,
    render_html,
    write_explorer,
)


def _trial(**over) -> dict:
    base = dict(
        config="local-only",
        task_id="t1",
        repeat_index=0,
        reward=1.0,
        score=1.0,
        status="completed",
        scorer_family="continuous",
        passed=True,
        sub_scores={"raw_score": 1.0},
        task_time_seconds=10.0,
        token_cost_usd=0.5,
        input_tokens=100,
        output_tokens=200,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        num_turns=5,
        result_subtype=None,
        hit_max_turns=False,
        tool_call_count=7,
        error_category=None,
        missing=False,
    )
    base.update(over)
    return base


def _write_run(tmp_path: Path, trials: list[dict], name: str = "run-x") -> Path:
    rd = tmp_path / "runs" / name
    rd.mkdir(parents=True)
    (rd / "per_trial.json").write_text(json.dumps(trials), encoding="utf-8")
    return rd


# ---------------------------------------------------------------------------
# Validity flags — structural only (A3)
# ---------------------------------------------------------------------------


class TestValidityFlags:
    def test_clean_completed_trial_has_no_flags(self) -> None:
        assert compute_validity_flags(_trial()) == []

    def test_error_status_and_category_flagged(self) -> None:
        f = compute_validity_flags(
            _trial(status="error", error_category="quota", reward=0)
        )
        assert "status:error" in f
        assert "error:quota" in f
        assert "zero-reward-noncomplete" in f

    def test_max_turns_flagged(self) -> None:
        assert "max-turns" in compute_validity_flags(_trial(hit_max_turns=True))

    def test_zero_tools_flagged_but_none_is_not(self) -> None:
        assert "zero-tools" in compute_validity_flags(_trial(tool_call_count=0))
        assert "zero-tools" not in compute_validity_flags(
            _trial(tool_call_count=None)
        )

    def test_completed_zero_reward_not_flagged_as_infra(self) -> None:
        # A genuine measured 0.0 on a completed trial is NOT an infra flag.
        f = compute_validity_flags(_trial(reward=0, status="completed"))
        assert "zero-reward-noncomplete" not in f

    def test_missing_flagged(self) -> None:
        assert "missing" in compute_validity_flags(_trial(missing=True))


# ---------------------------------------------------------------------------
# Per-arm summary (A4) — reuses stats math
# ---------------------------------------------------------------------------


class TestArmSummaries:
    def test_groups_by_arm_with_reused_stats(self) -> None:
        trials = [
            _trial(config="local-only", task_id="a", reward=1.0, token_cost_usd=1.0),
            _trial(config="local-only", task_id="b", reward=0.0, token_cost_usd=3.0),
            _trial(config="with-sg", task_id="a", reward=0.5, token_cost_usd=2.0),
        ]
        sums = {s.label: s for s in build_arm_summaries(trials)}
        assert set(sums) == {"local-only", "with-sg"}
        assert sums["local-only"].total == 2
        assert sums["local-only"].mean_reward == pytest.approx(0.5)
        assert sums["local-only"].median_reward == pytest.approx(0.5)
        assert sums["local-only"].mean_cost == pytest.approx(2.0)
        assert sums["with-sg"].mean_reward == pytest.approx(0.5)

    def test_flag_counts_per_arm(self) -> None:
        trials = [
            _trial(config="with-sg", task_id="a", status="error",
                   error_category="agent", reward=0),
            _trial(config="with-sg", task_id="b"),  # clean
        ]
        s = build_arm_summaries(trials)[0]
        assert s.flag_counts.get("status") == 1
        assert s.flag_counts.get("error") == 1
        assert s.total == 2  # one flagged + one clean

    def test_legacy_scoring_details_verdict_is_excluded(self) -> None:
        trials = [
            _trial(
                task_id="broken",
                reward=0.0,
                score=0.0,
                scoring_details={"passed": False, "verdict": "verifier_error"},
            ),
            _trial(
                task_id="wrong",
                reward=1.0,
                score=1.0,
                verdict="incorrect",
                scoring_details={"passed": True, "verdict": "incorrect"},
            ),
        ]

        summary = build_arm_summaries(trials)[0]

        assert summary.mean_reward == 1.0


# ---------------------------------------------------------------------------
# HTML generation (A1, A2, A5)
# ---------------------------------------------------------------------------


class TestHtmlGeneration:
    def test_writes_self_contained_html(self, tmp_path: Path) -> None:
        rd = _write_run(tmp_path, [_trial(), _trial(task_id="t2")])
        out = write_explorer(rd)
        assert out == rd / EXPLORER_FILE
        text = out.read_text()
        assert text.startswith("<!DOCTYPE html>")
        # No external network deps: no http(s) <script src> / <link href>.
        assert 'src="http' not in text and 'href="http' not in text
        assert "<script>" in text and "<style>" in text

    def test_table_contains_every_trial_and_flags(self, tmp_path: Path) -> None:
        trials = [
            _trial(task_id="good"),
            _trial(task_id="bad", status="error", error_category="quota", reward=0),
        ]
        rd = _write_run(tmp_path, trials)
        text = write_explorer(rd).read_text()
        assert "good" in text and "bad" in text
        # The trial data (incl. flags) is embedded for the JS layer.
        assert "status:error" in text
        assert "error:quota" in text

    def test_mixed_statuses_and_partial_fields_do_not_crash(
        self, tmp_path: Path
    ) -> None:
        # A5: missing/partial fields preserved, never dropped.
        trials = [
            _trial(task_id="ok"),
            _trial(task_id="err", status="error", error_category="agent", reward=0),
            {"config": "with-sg", "task_id": "partial", "status": "error"},  # sparse
            _trial(task_id="quota", status="error", error_category="quota",
                   reward=0, missing=True),
        ]
        rd = _write_run(tmp_path, trials)
        text = write_explorer(rd).read_text()
        assert "partial" in text  # sparse trial still rendered
        # sparse trial's preserved fields survive into the embedded JSON
        assert '"task_id": "partial"' in text or '"task_id":"partial"' in text

    def test_render_html_smoke_multi_arm(self) -> None:
        trials = [_trial(config="a"), _trial(config="b", task_id="t2")]
        sums = build_arm_summaries(trials)
        out = render_html("run-id", trials, sums)
        assert "run-id" in out
        assert out.count("<table") >= 2  # summary + trials


# ---------------------------------------------------------------------------
# Loading / discovery
# ---------------------------------------------------------------------------


class TestLoadAndDiscover:
    def test_load_preserves_all_fields(self, tmp_path: Path) -> None:
        rd = _write_run(tmp_path, [_trial(task_id="z", num_turns=99)])
        loaded = load_run_trials(rd)
        assert loaded[0]["num_turns"] == 99

    def test_load_rejects_non_list(self, tmp_path: Path) -> None:
        rd = tmp_path / "runs" / "bad"
        rd.mkdir(parents=True)
        (rd / "per_trial.json").write_text('{"not": "a list"}')
        with pytest.raises(ValueError):
            load_run_trials(rd)

    def test_newest_run_dir_picks_latest(self, tmp_path: Path) -> None:
        import os
        import time

        runs = tmp_path / "runs"
        old = runs / "old"
        new = runs / "new"
        for d in (old, new):
            d.mkdir(parents=True)
            (d / "per_trial.json").write_text(json.dumps([_trial()]))
        # Force a newer mtime on `new`'s per_trial.json.
        future = time.time() + 100
        os.utime(new / "per_trial.json", (future, future))
        assert newest_run_dir(runs) == new

    def test_newest_run_dir_raises_when_empty(self, tmp_path: Path) -> None:
        (tmp_path / "runs").mkdir()
        with pytest.raises(FileNotFoundError):
            newest_run_dir(tmp_path / "runs")
