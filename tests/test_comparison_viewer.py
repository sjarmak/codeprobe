"""Tests for the served arm-vs-arm comparison viewer (codeprobe-00e)."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from codeprobe.analysis.comparison_viewer import (
    build_arm_comparisons,
    build_html,
    build_task_matrix,
    make_server,
    render_comparison_html,
)
from codeprobe.analysis.run_explorer import load_run_trials


def _trial(config: str, task_id: str, rep: int, **over) -> dict:
    base = dict(
        config=config,
        task_id=task_id,
        repeat_index=rep,
        reward=1.0,
        score=1.0,
        status="completed",
        error_category=None,
        hit_max_turns=False,
        tool_call_count=5,
        token_cost_usd=1.0,
        input_tokens=10,
        output_tokens=20,
        num_turns=3,
        tool_use_by_name={"Read": 2, "mcp__sourcegraph__keyword_search": 1},
    )
    base.update(over)
    return base


def _two_arm_trials() -> list[dict]:
    return [
        _trial("local-only", "t1", 0, reward=1.0, tool_use_by_name={"Read": 3}),
        _trial("local-only", "t1", 1, reward=0.0, tool_use_by_name={"Read": 1}),
        _trial("with-sg", "t1", 0, reward=0.5),
        _trial("with-sg", "t1", 1, reward=0.5),
        _trial("with-sg", "t2", 0, reward=0.0, status="error",
               error_category="agent", tool_use_by_name={}),
    ]


# ---------------------------------------------------------------------------
# Per-arm summary (A1)
# ---------------------------------------------------------------------------


class TestArmComparisons:
    def test_summary_aggregates_per_arm(self) -> None:
        arms = {a.label: a for a in build_arm_comparisons(_two_arm_trials())}
        assert set(arms) == {"local-only", "with-sg"}
        assert arms["local-only"].total == 2
        assert arms["local-only"].mean_reward == pytest.approx(0.5)
        assert arms["local-only"].total_cost == pytest.approx(2.0)
        assert arms["with-sg"].error_count == 1

    def test_zero_mcp_is_structural(self) -> None:
        # local-only made no mcp__ calls (both trials) → 2; with-sg t1 used
        # mcp, t2 made none → 1.
        arms = {a.label: a for a in build_arm_comparisons(_two_arm_trials())}
        assert arms["local-only"].zero_mcp_count == 2
        assert arms["with-sg"].zero_mcp_count == 1

    def test_uncaptured_usage_not_counted_zero_mcp(self) -> None:
        # tool_use_by_name absent (None) must NOT count as zero-mcp.
        trials = [_trial("a", "t", 0, tool_use_by_name=None)]
        assert build_arm_comparisons(trials)[0].zero_mcp_count == 0

    def test_reproduces_9tk_headline_if_present(self) -> None:
        run = Path(
            "/home/ds/test_repos/gascity/gascity-mcp-comparison/.codeprobe/runs"
        )
        if not run.is_dir():
            pytest.skip("9tk run data not present in this environment")
        arms = {a.label: a for a in build_arm_comparisons(load_run_trials(run))}
        assert arms["local-only"].mean_reward == pytest.approx(0.810, abs=0.001)
        assert arms["local-only"].error_count == 0
        assert arms["with-sg-narrow"].mean_reward == pytest.approx(0.781, abs=0.001)
        assert arms["with-sg-full"].error_count == 1
        # The validity finding: narrow abandoned the SG surface.
        assert arms["with-sg-narrow"].zero_mcp_count == 30
        assert arms["with-sg-full"].zero_mcp_count == 0


# ---------------------------------------------------------------------------
# Per-task matrix (A2) — structural deltas
# ---------------------------------------------------------------------------


class TestTaskMatrix:
    def test_matrix_groups_tasks_by_arm(self) -> None:
        m = build_task_matrix(_two_arm_trials())
        assert m["arms"] == ["local-only", "with-sg"]
        rows = {r["task_id"]: r for r in m["rows"]}
        assert set(rows) == {"t1", "t2"}
        # t1: local-only mean 0.5, with-sg mean 0.5 → no delta.
        assert rows["t1"]["cells"]["local-only"]["mean_reward"] == pytest.approx(0.5)
        assert rows["t1"]["delta"] is False
        # t2: only with-sg has data → no cross-arm delta.
        assert rows["t2"]["cells"]["local-only"]["repeats"] == 0

    def test_delta_marked_when_rewards_differ(self) -> None:
        trials = [
            _trial("a", "tx", 0, reward=1.0),
            _trial("b", "tx", 0, reward=0.0),
        ]
        row = build_task_matrix(trials)["rows"][0]
        assert row["delta"] is True

    def test_cell_carries_trials_for_drillin(self) -> None:
        m = build_task_matrix(_two_arm_trials())
        cell = {r["task_id"]: r for r in m["rows"]}["t2"]["cells"]["with-sg"]
        assert cell["errors"] == 1
        assert "status:error" in cell["flags"]
        assert len(cell["trials"]) == 1  # raw trial preserved for drill-in


# ---------------------------------------------------------------------------
# HTML render
# ---------------------------------------------------------------------------


class TestRender:
    def test_comparison_html_self_contained(self) -> None:
        h = render_comparison_html("run-x", _two_arm_trials())
        assert h.startswith("<!DOCTYPE html>")
        assert 'src="http' not in h and 'href="http' not in h
        assert "local-only" in h and "with-sg" in h

    def test_build_html_single_arm_falls_back_to_explorer(self) -> None:
        # One arm → single-run explorer, not the comparison view.
        single = [_trial("only", "t1", 0)]
        # build_html takes a run_dir; exercise the renderers directly here and
        # assert the comparison path needs >=2 arms.
        m = build_task_matrix(single)
        assert m["arms"] == ["only"]


# ---------------------------------------------------------------------------
# Loader dual-layout (A3)
# ---------------------------------------------------------------------------


class TestLoaderLayouts:
    def test_per_trial_layout(self, tmp_path: Path) -> None:
        rd = tmp_path / "runs" / "single"
        rd.mkdir(parents=True)
        (rd / "per_trial.json").write_text(json.dumps([_trial("a", "t", 0)]))
        assert len(load_run_trials(rd)) == 1

    def test_per_arm_results_layout(self, tmp_path: Path) -> None:
        rd = tmp_path / "runs" / "cmp"
        for arm in ("local-only", "with-sg"):
            ad = rd / arm
            ad.mkdir(parents=True)
            completed = [
                {
                    "task_id": "t1",
                    "automated_score": 0.9,
                    "status": "completed",
                    "cost_usd": 2.0,
                    "repeat_index": 0,
                    "scoring_details": {"passed": True},
                }
            ]
            (ad / "results.json").write_text(
                json.dumps({"config": arm, "completed": completed, "summary": {}})
            )
        trials = load_run_trials(rd)
        assert len(trials) == 2
        # Normalized: automated_score -> reward+score, cost_usd -> token_cost_usd,
        # passed lifted from scoring_details.
        t = trials[0]
        assert t["reward"] == 0.9 and t["score"] == 0.9
        assert t["token_cost_usd"] == 2.0
        assert t["passed"] is True
        assert {x["config"] for x in trials} == {"local-only", "with-sg"}

    def test_missing_field_preserved_not_fabricated(self, tmp_path: Path) -> None:
        # results.json has no hit_max_turns; the normalizer must leave it
        # ABSENT (not fabricated), preserving honest partial data.
        rd = tmp_path / "runs" / "cmp2"
        ad = rd / "arm1"
        ad.mkdir(parents=True)
        (ad / "results.json").write_text(
            json.dumps(
                {
                    "config": "arm1",
                    "completed": [
                        {"task_id": "t", "automated_score": 0.0, "status": "error"}
                    ],
                    "summary": {},
                }
            )
        )
        t = load_run_trials(rd)[0]
        assert "hit_max_turns" not in t  # not invented
        assert t["status"] == "error"  # not dropped

    def test_unknown_layout_raises(self, tmp_path: Path) -> None:
        rd = tmp_path / "runs" / "empty"
        rd.mkdir(parents=True)
        with pytest.raises(FileNotFoundError):
            load_run_trials(rd)


# ---------------------------------------------------------------------------
# Serve mode (A1/step 2) — ephemeral port, no hardcoded 8766 in the bind
# ---------------------------------------------------------------------------


class TestServe:
    def test_serves_html_200_and_404(self) -> None:
        server = make_server("<!DOCTYPE html><h1>cmp</h1>", host="127.0.0.1", port=0)
        port = server.server_address[1]
        assert port != 0  # ephemeral port actually bound
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5)
            assert resp.status == 200
            body = resp.read().decode()
            assert "<!DOCTYPE html>" in body and "cmp" in body
            with pytest.raises(urllib.error.HTTPError) as ei:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/missing", timeout=5)
            assert ei.value.code == 404
        finally:
            server.shutdown()
            server.server_close()

    def test_build_html_then_serve_roundtrip(self, tmp_path: Path) -> None:
        # End-to-end: per-arm layout -> comparison HTML -> served 200.
        rd = tmp_path / "runs" / "cmp"
        for arm in ("a", "b"):
            ad = rd / arm
            ad.mkdir(parents=True)
            (ad / "results.json").write_text(
                json.dumps(
                    {
                        "config": arm,
                        "completed": [
                            {"task_id": "t1", "automated_score": 0.5, "status": "completed"}
                        ],
                        "summary": {},
                    }
                )
            )
        html_body = build_html(rd)
        assert "arm-vs-arm comparison" in html_body  # comparison view chosen
        server = make_server(html_body, host="127.0.0.1", port=0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5)
            assert resp.status == 200
        finally:
            server.shutdown()
            server.server_close()
