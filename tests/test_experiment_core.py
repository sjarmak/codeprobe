"""Tests for core/experiment.py — experiment directory I/O."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeprobe.core.experiment import (
    append_checkpoint,
    create_experiment_dir,
    ensure_default_experiment,
    load_checkpoint,
    load_config_results,
    load_experiment,
    save_config_results,
    save_experiment,
)
from codeprobe.models.experiment import (
    CompletedTask,
    ConfigResults,
    Experiment,
    ExperimentConfig,
)


def _sample_experiment() -> Experiment:
    return Experiment(
        name="test-exp",
        description="A test experiment",
        configs=[
            ExperimentConfig(label="baseline"),
            ExperimentConfig(label="variant", model="claude-sonnet-4-6"),
        ],
        tasks_dir="tasks",
    )


def test_create_experiment_dir(tmp_path: Path):
    exp = _sample_experiment()
    exp_dir = create_experiment_dir(tmp_path, exp)

    assert exp_dir.is_dir()
    assert (exp_dir / "tasks").is_dir()
    assert (exp_dir / "runs" / "baseline").is_dir()
    assert (exp_dir / "runs" / "variant").is_dir()
    assert (exp_dir / "experiment.json").is_file()

    data = json.loads((exp_dir / "experiment.json").read_text())
    assert data["name"] == "test-exp"


def test_save_and_load_experiment(tmp_path: Path):
    exp = _sample_experiment()
    exp_dir = create_experiment_dir(tmp_path, exp)

    loaded = load_experiment(exp_dir)
    assert loaded.name == exp.name
    assert loaded.description == exp.description
    assert len(loaded.configs) == 2
    assert loaded.configs[0].label == "baseline"


def test_save_and_load_experiment_default_bias_thresholds(tmp_path: Path):
    """codeprobe-kdng: defaults round-trip through experiment.json."""
    exp = _sample_experiment()
    exp_dir = create_experiment_dir(tmp_path, exp)

    loaded = load_experiment(exp_dir)
    assert loaded.bias_overshipping_recall_min == 0.95
    assert loaded.bias_overshipping_low_precision_max == 0.5
    assert loaded.bias_overshipping_precision_gap_min == 0.3
    assert loaded.configs[0].low_confidence_threshold == 0.5


def test_save_and_load_experiment_custom_bias_thresholds(tmp_path: Path):
    """codeprobe-kdng: non-default thresholds round-trip through
    experiment.json — the config surface is real, not decorative."""
    exp = Experiment(
        name="test-exp",
        configs=[
            ExperimentConfig(label="baseline", low_confidence_threshold=0.7),
        ],
        bias_overshipping_recall_min=0.8,
        bias_overshipping_low_precision_max=0.4,
        bias_overshipping_precision_gap_min=0.2,
    )
    exp_dir = create_experiment_dir(tmp_path, exp)

    loaded = load_experiment(exp_dir)
    assert loaded.bias_overshipping_recall_min == 0.8
    assert loaded.bias_overshipping_low_precision_max == 0.4
    assert loaded.bias_overshipping_precision_gap_min == 0.2
    assert loaded.configs[0].low_confidence_threshold == 0.7


def test_load_experiment_missing_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_experiment(tmp_path / "nonexistent")


def test_save_experiment_overwrites(tmp_path: Path):
    exp = _sample_experiment()
    exp_dir = create_experiment_dir(tmp_path, exp)

    updated = Experiment(
        name="test-exp",
        description="Updated description",
        configs=exp.configs,
    )
    save_experiment(exp_dir, updated)
    loaded = load_experiment(exp_dir)
    assert loaded.description == "Updated description"


def test_save_and_load_config_results(tmp_path: Path):
    exp = _sample_experiment()
    exp_dir = create_experiment_dir(tmp_path, exp)

    completed = [
        CompletedTask(task_id="t-001", automated_score=1.0, duration_seconds=2.5),
        CompletedTask(task_id="t-002", automated_score=0.0, duration_seconds=1.0),
    ]

    path = save_config_results(exp_dir, "baseline", completed)
    assert path.is_file()

    loaded = load_config_results(exp_dir, "baseline")
    assert isinstance(loaded, ConfigResults)
    assert loaded.config == "baseline"
    assert len(loaded.completed) == 2
    assert loaded.completed[0].task_id == "t-001"
    assert loaded.completed[0].automated_score == 1.0


def test_config_results_roundtrip_preserves_all_fields(tmp_path: Path):
    """Every CompletedTask field survives save/load (codeprobe-8up).

    Guards against the stale-hand-enumeration bug class: a field added
    to the dataclass but forgotten in a loader was silently dropped.
    """
    exp = _sample_experiment()
    exp_dir = create_experiment_dir(tmp_path, exp)

    original = CompletedTask(
        task_id="t-full",
        automated_score=0.83,
        repeat_index=2,
        status="failed",
        duration_seconds=2032.0,
        input_tokens=54321,
        output_tokens=9876,
        cost_usd=6.99,
        cost_model="per_token",
        cost_source="api_reported",
        tool_call_count=99,
        tool_use_by_name={"Read": 5, "Grep": 2},
        num_turns=90,
        result_subtype="error_max_turns",
        duration_api_ms=1854321,
        error_category="agent",
        scoring_details={"passed": False, "error": None},
        metadata={"error": "Reached maximum number of turns (90)"},
    )
    save_config_results(exp_dir, "baseline", [original])

    loaded = load_config_results(exp_dir, "baseline").completed[0]
    assert loaded == original


def test_load_config_results_missing_raises(tmp_path: Path):
    exp = _sample_experiment()
    exp_dir = create_experiment_dir(tmp_path, exp)

    with pytest.raises(FileNotFoundError):
        load_config_results(exp_dir, "nonexistent")


def test_save_config_results_aggregates_oracle_metrics(tmp_path: Path):
    """Per-config summary must surface mean precision/recall/f1 when present.

    Oracle-scored tasks (file_list, symbol_list, etc.) emit precision and
    recall via scoring_details. The summary block in results.json now means
    these — without this, an aggregate report can show two configs at the
    same F1 while one has P=0.26/R=1.0 (brute-force) and the other has
    P=0.7/R=0.6 (careful), and there's no way to tell from the JSON.
    """
    exp = _sample_experiment()
    exp_dir = create_experiment_dir(tmp_path, exp)

    completed = [
        CompletedTask(
            task_id="t-001",
            automated_score=0.4092,
            duration_seconds=77.0,
            cost_usd=0.84,
            scoring_details={
                "f1": 0.4092,
                "precision": 0.2571,
                "recall": 1.0,
                "matched": 80,
                "expected_count": 80,
                "agent_files_count": 311,
            },
        ),
        CompletedTask(
            task_id="t-002",
            automated_score=0.6,
            duration_seconds=50.0,
            cost_usd=0.5,
            scoring_details={
                "f1": 0.6,
                "precision": 0.55,
                "recall": 0.66,
                "matched": 33,
                "expected_count": 50,
                "agent_files_count": 60,
            },
        ),
    ]

    path = save_config_results(exp_dir, "baseline", completed)
    payload = json.loads(path.read_text())
    summary = payload["summary"]

    # F1 stays the headline number alongside the score mean
    assert summary["mean_automated_score"] == pytest.approx((0.4092 + 0.6) / 2, abs=1e-3)
    assert summary["mean_precision"] == pytest.approx((0.2571 + 0.55) / 2, abs=1e-4)
    assert summary["mean_recall"] == pytest.approx((1.0 + 0.66) / 2, abs=1e-4)
    assert summary["mean_f1"] == pytest.approx((0.4092 + 0.6) / 2, abs=1e-3)


def test_compute_summary_excludes_quota_casualties_from_mean(tmp_path: Path):
    """mean_automated_score is computed over real trials only (codeprobe-9jxx).

    Quota-errored trials are stamped automated_score=0.0 by the executor; that
    0.0 is an infrastructure failure, not a quality measurement, so it must not
    drag the published mean toward zero. The quota count is surfaced separately,
    and cost/token totals stay over all attempts (real billed work).
    """
    exp = _sample_experiment()
    exp_dir = create_experiment_dir(tmp_path, exp)

    completed = [
        CompletedTask(
            task_id="t-001", automated_score=1.0, duration_seconds=2.0, cost_usd=0.5
        ),
        CompletedTask(
            task_id="t-002", automated_score=0.5, duration_seconds=4.0, cost_usd=0.5
        ),
        CompletedTask(
            task_id="t-003",
            automated_score=0.0,
            duration_seconds=1.0,
            cost_usd=0.2,
            error_category="quota",
        ),
    ]

    path = save_config_results(exp_dir, "baseline", completed)
    summary = json.loads(path.read_text())["summary"]

    # Mean over the two real trials only — the 0.0 quota stub is excluded.
    assert summary["mean_automated_score"] == pytest.approx((1.0 + 0.5) / 2)
    # Structural total still counts every completed trial.
    assert summary["tasks_completed"] == 3
    # The quota count is surfaced so the exclusion is auditable.
    assert summary["quota_error_count"] == 1
    # Duration is summed over the reward population only.
    assert summary["total_duration_seconds"] == pytest.approx(6.0)
    # Cost stays over all attempts — quota trials still cost real money.
    assert summary["total_cost_usd"] == pytest.approx(0.5 + 0.5 + 0.2)


def test_compute_summary_all_quota_yields_zero_mean(tmp_path: Path):
    """A config where every trial is a quota casualty reports mean 0.0, no crash."""
    exp = _sample_experiment()
    exp_dir = create_experiment_dir(tmp_path, exp)

    completed = [
        CompletedTask(
            task_id="t-001",
            automated_score=0.0,
            duration_seconds=1.0,
            error_category="quota",
        ),
        CompletedTask(
            task_id="t-002",
            automated_score=0.0,
            duration_seconds=1.0,
            error_category="quota",
        ),
    ]

    path = save_config_results(exp_dir, "baseline", completed)
    summary = json.loads(path.read_text())["summary"]

    assert summary["mean_automated_score"] == 0.0
    assert summary["quota_error_count"] == 2
    assert summary["tasks_completed"] == 2
    assert "score_per_dollar" not in summary


def test_save_config_results_omits_metrics_when_no_oracle_data(tmp_path: Path):
    """Backward compat: tasks without scoring_details produce no P/R fields."""
    exp = _sample_experiment()
    exp_dir = create_experiment_dir(tmp_path, exp)

    completed = [
        CompletedTask(task_id="t-001", automated_score=1.0, duration_seconds=2.5),
        CompletedTask(task_id="t-002", automated_score=0.0, duration_seconds=1.0),
    ]
    path = save_config_results(exp_dir, "baseline", completed)
    summary = json.loads(path.read_text())["summary"]

    assert "mean_precision" not in summary
    assert "mean_recall" not in summary
    assert "mean_f1" not in summary


def test_save_and_load_task_ids(tmp_path: Path):
    """Experiment.task_ids round-trips through save/load."""
    exp = Experiment(
        name="scoped-exp",
        description="Experiment with scoped tasks",
        configs=[ExperimentConfig(label="baseline")],
        task_ids=("aaa111", "bbb222"),
    )
    exp_dir = create_experiment_dir(tmp_path, exp)

    loaded = load_experiment(exp_dir)
    assert loaded.task_ids == ("aaa111", "bbb222")


def test_load_experiment_without_task_ids(tmp_path: Path):
    """Old experiment.json without task_ids loads with empty tuple."""
    exp = _sample_experiment()
    exp_dir = create_experiment_dir(tmp_path, exp)

    # Manually strip task_ids from the JSON to simulate old format
    path = exp_dir / "experiment.json"
    data = json.loads(path.read_text())
    data.pop("task_ids", None)
    path.write_text(json.dumps(data))

    loaded = load_experiment(exp_dir)
    assert loaded.task_ids == ()


def test_task_ids_filters_discovery(tmp_path: Path):
    """When task_ids is set, only those tasks are discovered by _find_tasks."""
    from codeprobe.cli.run_cmd import _find_tasks

    # Create 3 task dirs
    tasks_dir = tmp_path / "tasks"
    for tid in ("aaa111", "bbb222", "ccc333"):
        d = tasks_dir / tid
        d.mkdir(parents=True)
        (d / "instruction.md").write_text("do something")

    # Without filter: all 3
    all_tasks = _find_tasks(tasks_dir)
    assert len(all_tasks) == 3

    # With filter: only 2
    filtered = _find_tasks(tasks_dir, task_ids=("aaa111", "ccc333"))
    assert [d.name for d in filtered] == ["aaa111", "ccc333"]


def test_task_ids_filters_ignores_missing(tmp_path: Path):
    """task_ids referencing non-existent dirs are silently skipped."""
    from codeprobe.cli.run_cmd import _find_tasks

    tasks_dir = tmp_path / "tasks"
    d = tasks_dir / "aaa111"
    d.mkdir(parents=True)
    (d / "instruction.md").write_text("do something")

    filtered = _find_tasks(tasks_dir, task_ids=("aaa111", "missing999"))
    assert [d.name for d in filtered] == ["aaa111"]


def test_ensure_default_experiment_prefers_direct_location(tmp_path: Path):
    """A direct .codeprobe/experiment.json wins and is returned as-is."""
    codeprobe_dir = tmp_path / ".codeprobe"
    codeprobe_dir.mkdir()
    save_experiment(codeprobe_dir, Experiment(name="mine-exp"))

    result = ensure_default_experiment(tmp_path)

    assert result == codeprobe_dir
    # Existing experiment untouched — not overwritten by a "default" one.
    assert load_experiment(codeprobe_dir).name == "mine-exp"


def test_ensure_default_experiment_single_named_subdir(tmp_path: Path):
    """Exactly one named subdir with experiment.json is resolved."""
    codeprobe_dir = tmp_path / ".codeprobe"
    exp_dir = create_experiment_dir(codeprobe_dir, Experiment(name="only-one"))

    result = ensure_default_experiment(tmp_path)

    assert result == exp_dir
    assert load_experiment(exp_dir).name == "only-one"


def test_ensure_default_experiment_ambiguous_raises(tmp_path: Path):
    """Multiple named subdirs: raise, never create, modify, or guess."""
    codeprobe_dir = tmp_path / ".codeprobe"
    for name in ("exp-a", "exp-b"):
        create_experiment_dir(codeprobe_dir, Experiment(name=name))

    with pytest.raises(ValueError, match="Multiple experiments"):
        ensure_default_experiment(tmp_path)

    assert not (codeprobe_dir / "experiment.json").exists()
    for name in ("exp-a", "exp-b"):
        assert load_experiment(codeprobe_dir / name).task_ids == ()


def test_ensure_default_experiment_creates_default(tmp_path: Path):
    """No experiment anywhere: create .codeprobe/experiment.json."""
    result = ensure_default_experiment(tmp_path, description="Auto-created by codeprobe mine")

    codeprobe_dir = tmp_path / ".codeprobe"
    assert result == codeprobe_dir
    experiment = load_experiment(codeprobe_dir)
    assert experiment.name == "default"
    assert experiment.description == "Auto-created by codeprobe mine"
    assert experiment.configs == []
    assert (codeprobe_dir / "tasks").is_dir()


def test_record_task_ids_in_experiment(tmp_path: Path):
    """_record_task_ids_in_experiment updates experiment.json with task IDs."""
    from codeprobe.cli.mine_cmd import _record_task_ids_in_experiment

    # Set up repo with a single experiment
    codeprobe_dir = tmp_path / ".codeprobe"
    exp = Experiment(
        name="my-exp",
        configs=[ExperimentConfig(label="baseline")],
    )
    exp_dir = create_experiment_dir(codeprobe_dir, exp)

    # Record task IDs
    _record_task_ids_in_experiment(tmp_path, ["ccc333", "aaa111", "bbb222"])

    loaded = load_experiment(exp_dir)
    assert loaded.task_ids == ("aaa111", "bbb222", "ccc333")  # sorted


def test_record_task_ids_unions_and_persists(tmp_path: Path):
    """core record_task_ids unions new ids with existing ones and saves."""
    from codeprobe.core.experiment import record_task_ids

    exp_dir = tmp_path / ".codeprobe"
    exp_dir.mkdir()
    save_experiment(exp_dir, Experiment(name="default", task_ids=("old-1",)))

    updated = record_task_ids(exp_dir, ["new-2", "new-1", "old-1"])

    assert updated.task_ids == ("new-1", "new-2", "old-1")
    assert load_experiment(exp_dir).task_ids == ("new-1", "new-2", "old-1")


def test_record_task_ids_missing_experiment_raises(tmp_path: Path):
    from codeprobe.core.experiment import record_task_ids

    with pytest.raises(FileNotFoundError):
        record_task_ids(tmp_path, ["t-1"])


def test_record_task_ids_skips_multiple_experiments(tmp_path: Path):
    """No update when multiple experiments exist (ambiguous)."""
    from codeprobe.cli.mine_cmd import _record_task_ids_in_experiment

    codeprobe_dir = tmp_path / ".codeprobe"
    for name in ("exp-a", "exp-b"):
        create_experiment_dir(
            codeprobe_dir,
            Experiment(name=name, configs=[ExperimentConfig(label="base")]),
        )

    _record_task_ids_in_experiment(tmp_path, ["task1"])

    # Neither experiment should have task_ids set
    for name in ("exp-a", "exp-b"):
        loaded = load_experiment(codeprobe_dir / name)
        assert loaded.task_ids == ()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_append_and_load_checkpoint(tmp_path: Path):
    checkpoint = tmp_path / "checkpoint.jsonl"

    t1 = CompletedTask(task_id="t-001", automated_score=1.0)
    t2 = CompletedTask(task_id="t-002", automated_score=0.0)

    append_checkpoint(checkpoint, t1)
    append_checkpoint(checkpoint, t2)

    ids = load_checkpoint(checkpoint)
    assert ids == {"t-001", "t-002"}


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_load_checkpoint_empty(tmp_path: Path):
    checkpoint = tmp_path / "checkpoint.jsonl"
    ids = load_checkpoint(checkpoint)
    assert ids == set()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_load_checkpoint_skips_malformed(tmp_path: Path):
    checkpoint = tmp_path / "checkpoint.jsonl"
    checkpoint.write_text(
        '{"task_id": "t-001"}\n' "not valid json\n" '{"task_id": "t-002"}\n'
    )
    ids = load_checkpoint(checkpoint)
    assert ids == {"t-001", "t-002"}
