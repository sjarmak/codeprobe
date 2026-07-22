"""Tests for codeprobe experiment CLI command group."""

from __future__ import annotations

import json
import statistics
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli import main
from codeprobe.core.experiment import (
    create_experiment_dir,
    load_experiment,
    save_config_results,
    save_experiment,
)
from codeprobe.models.experiment import (
    CompletedTask,
    Experiment,
    ExperimentConfig,
)


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def exp_dir(tmp_path: Path) -> Path:
    """Create a minimal experiment directory with tasks and configs."""
    exp = Experiment(
        name="test-exp",
        description="A test experiment",
        configs=[
            ExperimentConfig(label="baseline", agent="claude"),
            ExperimentConfig(
                label="variant", agent="claude", model="claude-sonnet-4-6"
            ),
        ],
        tasks_dir="tasks",
    )
    d = create_experiment_dir(tmp_path, exp)
    # Create two task directories with instruction.md and test.sh.
    # confidence.json is required by the promotion gate added in
    # codeprobe-9fri (calibration triad gates per task) — without it
    # the task would default to confidence=0.30 and be quarantined,
    # making the validate_ready fixture fail. Real mined tasks always
    # carry confidence.json so the fixture mirrors that contract.
    import json as _json

    for tid in ("task-001", "task-002"):
        task_dir = d / "tasks" / tid
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "instruction.md").write_text(f"# {tid}\nDo something.\n")
        tests_dir = task_dir / "tests"
        tests_dir.mkdir()
        (tests_dir / "test.sh").write_text("#!/bin/bash\nexit 0\n")
        (task_dir / "confidence.json").write_text(
            _json.dumps(
                {
                    "task_id": tid,
                    "score": 0.85,
                    "threshold": 0.5,
                    "breakdown": {},
                    "notes": {},
                    "promoted": True,
                }
            )
        )
    return d


# ---- experiment command is registered ----


def test_experiment_command_registered(runner: CliRunner) -> None:
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "experiment" in result.output


def test_experiment_help(runner: CliRunner) -> None:
    result = runner.invoke(main, ["experiment", "--help"])
    assert result.exit_code == 0
    for sub in ("init", "add-config", "validate", "status", "aggregate"):
        assert sub in result.output, f"Subcommand '{sub}' not in experiment help"


# ---- init ----


def test_init_creates_experiment(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        main,
        [
            "experiment",
            "init",
            str(tmp_path),
            "--name",
            "my-exp",
            "--description",
            "testing",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "my-exp" in result.output

    exp_dir = tmp_path / "my-exp"
    assert exp_dir.is_dir()
    assert (exp_dir / "experiment.json").is_file()
    assert (exp_dir / "tasks").is_dir()

    loaded = load_experiment(exp_dir)
    assert loaded.name == "my-exp"
    assert loaded.description == "testing"


def test_init_fails_if_exists(runner: CliRunner, exp_dir: Path) -> None:
    parent = exp_dir.parent
    result = runner.invoke(
        main,
        ["experiment", "init", str(parent), "--name", "test-exp"],
    )
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_init_rejects_unsafe_name(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        main,
        ["experiment", "init", str(tmp_path), "--name", "../traversal"],
    )
    assert result.exit_code == 1
    assert "Unsafe" in result.output or "error" in result.output.lower()


# ---- init --non-interactive (BUG-INIT-DEFAULT-006) ----


def test_init_non_interactive_creates_experiment_json(
    runner: CliRunner, tmp_path: Path
) -> None:
    """--non-interactive writes .codeprobe/experiment.json inside the target path."""
    result = runner.invoke(
        main,
        ["experiment", "init", str(tmp_path), "--non-interactive"],
    )
    assert result.exit_code == 0, result.output

    codeprobe_dir = tmp_path / ".codeprobe"
    exp_json = codeprobe_dir / "experiment.json"
    assert codeprobe_dir.is_dir(), ".codeprobe/ directory not created"
    assert exp_json.is_file(), ".codeprobe/experiment.json not created"
    assert (codeprobe_dir / "tasks").is_dir(), ".codeprobe/tasks/ not created"

    loaded = load_experiment(codeprobe_dir)
    assert loaded.name == "default"


def test_init_non_interactive_with_custom_name(
    runner: CliRunner, tmp_path: Path
) -> None:
    """--non-interactive respects --name when provided."""
    result = runner.invoke(
        main,
        [
            "experiment",
            "init",
            str(tmp_path),
            "--non-interactive",
            "--name",
            "custom-exp",
        ],
    )
    assert result.exit_code == 0, result.output

    loaded = load_experiment(tmp_path / ".codeprobe")
    assert loaded.name == "custom-exp"


def test_init_non_interactive_fails_if_already_exists(
    runner: CliRunner, tmp_path: Path
) -> None:
    """--non-interactive refuses to overwrite an existing experiment."""
    # First init
    runner.invoke(
        main,
        ["experiment", "init", str(tmp_path), "--non-interactive"],
    )
    # Second init should fail
    result = runner.invoke(
        main,
        ["experiment", "init", str(tmp_path), "--non-interactive"],
    )
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_init_non_interactive_experiment_json_is_valid(
    runner: CliRunner, tmp_path: Path
) -> None:
    """The generated experiment.json is valid JSON with expected structure."""
    result = runner.invoke(
        main,
        ["experiment", "init", str(tmp_path), "--non-interactive"],
    )
    assert result.exit_code == 0, result.output
    exp_json = tmp_path / ".codeprobe" / "experiment.json"
    data = json.loads(exp_json.read_text())
    assert "name" in data
    assert "configs" in data
    assert "tasks_dir" in data


# ---- add-config ----


def test_add_config(runner: CliRunner, exp_dir: Path) -> None:
    result = runner.invoke(
        main,
        [
            "experiment",
            "add-config",
            str(exp_dir),
            "--label",
            "with-mcp",
            "--agent",
            "claude",
            "--model",
            "claude-sonnet-4-6",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "with-mcp" in result.output

    loaded = load_experiment(exp_dir)
    labels = [c.label for c in loaded.configs]
    assert "with-mcp" in labels


def test_add_config_duplicate_label(runner: CliRunner, exp_dir: Path) -> None:
    result = runner.invoke(
        main,
        [
            "experiment",
            "add-config",
            str(exp_dir),
            "--label",
            "baseline",
            "--agent",
            "claude",
        ],
    )
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_add_config_rejects_unsafe_label(runner: CliRunner, exp_dir: Path) -> None:
    result = runner.invoke(
        main,
        [
            "experiment",
            "add-config",
            str(exp_dir),
            "--label",
            "../bad",
        ],
    )
    assert result.exit_code == 1


def test_add_config_codex_quarantined(runner: CliRunner, exp_dir: Path) -> None:
    """codeprobe-f7rl.27: a config that can never run must not be persisted.

    The codex adapter is quarantined (single-shot completion API, no
    workspace access); add-config refuses it with the same message run
    preflight uses, and nothing is written to experiment.json.
    """
    result = runner.invoke(
        main,
        [
            "experiment",
            "add-config",
            str(exp_dir),
            "--label",
            "codex-arm",
            "--agent",
            "codex",
        ],
    )
    assert result.exit_code == 1
    assert "quarantined" in result.output
    assert "cannot edit files" in result.output
    assert "claude" in result.output  # the alternative is named

    loaded = load_experiment(exp_dir)
    assert "codex-arm" not in [c.label for c in loaded.configs]


# ---- validate ----


def test_validate_ready(runner: CliRunner, exp_dir: Path) -> None:
    result = runner.invoke(main, ["experiment", "validate", str(exp_dir)])
    assert result.exit_code == 0
    assert "READY" in result.output


def test_validate_no_tasks(runner: CliRunner, tmp_path: Path) -> None:
    """Experiment with configs but no tasks should report errors."""
    exp = Experiment(
        name="empty-exp",
        configs=[ExperimentConfig(label="baseline")],
    )
    d = create_experiment_dir(tmp_path, exp)
    result = runner.invoke(main, ["experiment", "validate", str(d)])
    assert result.exit_code == 1
    assert "No tasks" in result.output


def test_validate_no_configs(runner: CliRunner, tmp_path: Path) -> None:
    """Experiment with tasks but no configs should report errors."""
    exp = Experiment(name="no-cfg-exp", configs=[])
    d = create_experiment_dir(tmp_path, exp)
    # Add a task
    task_dir = d / "tasks" / "t1"
    task_dir.mkdir(parents=True)
    (task_dir / "instruction.md").write_text("# Task\n")
    result = runner.invoke(main, ["experiment", "validate", str(d)])
    assert result.exit_code == 1
    assert "No configurations" in result.output


# ---- status ----


def test_status_shows_configs(runner: CliRunner, exp_dir: Path) -> None:
    result = runner.invoke(main, ["experiment", "status", str(exp_dir)])
    assert result.exit_code == 0
    assert "baseline" in result.output
    assert "variant" in result.output


def test_status_with_results(runner: CliRunner, exp_dir: Path) -> None:
    completed = [
        CompletedTask(task_id="task-001", automated_score=1.0, duration_seconds=2.0),
        CompletedTask(task_id="task-002", automated_score=0.5, duration_seconds=3.0),
    ]
    save_config_results(exp_dir, "baseline", completed)

    result = runner.invoke(main, ["experiment", "status", str(exp_dir)])
    assert result.exit_code == 0
    assert "baseline" in result.output


# ---- aggregate ----


def test_aggregate_produces_report(runner: CliRunner, exp_dir: Path) -> None:
    for label in ("baseline", "variant"):
        completed = [
            CompletedTask(
                task_id="task-001",
                automated_score=1.0 if label == "baseline" else 0.5,
                duration_seconds=2.0,
                cost_usd=0.10,
            ),
            CompletedTask(
                task_id="task-002",
                automated_score=0.5,
                duration_seconds=3.0,
                cost_usd=0.15,
            ),
        ]
        save_config_results(exp_dir, label, completed)

    result = runner.invoke(main, ["experiment", "aggregate", str(exp_dir)])
    assert result.exit_code == 0, result.output

    report_path = exp_dir / "reports" / "aggregate.json"
    assert report_path.is_file()

    report = json.loads(report_path.read_text())
    assert report["experiment"] == "test-exp"
    assert "config_summaries" in report
    assert "baseline" in report["config_summaries"]


def test_aggregate_emits_token_count_rollups(
    runner: CliRunner, exp_dir: Path
) -> None:
    """codeprobe-oktg: aggregate.json.config_summaries carries
    total_input_tokens / total_output_tokens / mean_*_per_task alongside
    the existing cost rollup. Cost-Pareto plots that aren't dollar-locked
    need the raw counts."""
    save_config_results(
        exp_dir,
        "baseline",
        [
            CompletedTask(
                task_id="task-001",
                automated_score=1.0,
                duration_seconds=2.0,
                cost_usd=0.10,
                input_tokens=1000,
                output_tokens=200,
            ),
            CompletedTask(
                task_id="task-002",
                automated_score=0.5,
                duration_seconds=3.0,
                cost_usd=0.15,
                input_tokens=2000,
                output_tokens=400,
            ),
        ],
    )
    save_config_results(
        exp_dir,
        "variant",
        [
            CompletedTask(
                task_id="task-001",
                automated_score=1.0,
                duration_seconds=2.0,
                cost_usd=0.10,
                # Mixed availability: one task missing usage. Mean should
                # be over the populated tasks only, not coerce missing to 0.
                input_tokens=500,
                output_tokens=100,
            ),
            CompletedTask(
                task_id="task-002",
                automated_score=0.5,
                duration_seconds=3.0,
                cost_usd=0.15,
            ),
        ],
    )

    result = runner.invoke(main, ["experiment", "aggregate", str(exp_dir)])
    assert result.exit_code == 0, result.output

    report = json.loads((exp_dir / "reports" / "aggregate.json").read_text())
    summaries = report["config_summaries"]

    base = summaries["baseline"]
    assert base["total_input_tokens"] == 3000
    assert base["total_output_tokens"] == 600
    assert base["mean_input_tokens_per_task"] == pytest.approx(1500.0)
    assert base["mean_output_tokens_per_task"] == pytest.approx(300.0)
    # No regression on existing cost rollup.
    assert base["total_cost_usd"] == pytest.approx(0.25)

    var = summaries["variant"]
    # Only one task in variant reported usage → totals/means reflect that.
    assert var["total_input_tokens"] == 500
    assert var["total_output_tokens"] == 100
    assert var["mean_input_tokens_per_task"] == pytest.approx(500.0)
    assert var["mean_output_tokens_per_task"] == pytest.approx(100.0)


def test_aggregate_excludes_quota_from_headline_mean(
    runner: CliRunner, exp_dir: Path
) -> None:
    """codeprobe-9jxx: the headline mean_automated_score / stdev in
    config_summaries must exclude quota casualties (executor-stamped
    automated_score=0.0), and surface a quota_error_count so the exclusion
    is auditable. Cost/token totals stay over all attempts."""
    save_config_results(
        exp_dir,
        "baseline",
        [
            CompletedTask(
                task_id="task-001",
                automated_score=1.0,
                duration_seconds=2.0,
                cost_usd=0.10,
            ),
            CompletedTask(
                task_id="task-002",
                automated_score=0.5,
                duration_seconds=3.0,
                cost_usd=0.10,
            ),
            CompletedTask(
                task_id="task-003",
                automated_score=0.0,
                duration_seconds=1.0,
                cost_usd=0.05,
                error_category="quota",
            ),
        ],
    )

    result = runner.invoke(main, ["experiment", "aggregate", str(exp_dir)])
    assert result.exit_code == 0, result.output

    report = json.loads((exp_dir / "reports" / "aggregate.json").read_text())
    base = report["config_summaries"]["baseline"]

    # Mean over the two real trials only — the 0.0 quota stub is excluded.
    assert base["mean_automated_score"] == pytest.approx((1.0 + 0.5) / 2)
    assert base["mean_reward"] == pytest.approx((1.0 + 0.5) / 2)
    # stdev is over the reward population too (two real scores).
    assert base["stdev_automated_score"] == pytest.approx(
        statistics.stdev([1.0, 0.5])
    )
    # Quota count surfaced; structural task total unchanged.
    assert base["quota_error_count"] == 1
    assert base["tasks_completed"] == 3
    # Cost stays over all attempts.
    assert base["total_cost_usd"] == pytest.approx(0.25)


def test_aggregate_token_rollups_none_when_unavailable(
    runner: CliRunner, exp_dir: Path
) -> None:
    """When no task in a config reports token counts, the rollups are
    None (omitted-as-zero would be a verifier-honesty violation)."""
    save_config_results(
        exp_dir,
        "baseline",
        [
            CompletedTask(
                task_id="task-001",
                automated_score=1.0,
                duration_seconds=2.0,
                cost_usd=0.10,
            ),
        ],
    )
    result = runner.invoke(main, ["experiment", "aggregate", str(exp_dir)])
    assert result.exit_code == 0, result.output

    report = json.loads((exp_dir / "reports" / "aggregate.json").read_text())
    base = report["config_summaries"]["baseline"]
    assert base["total_input_tokens"] is None
    assert base["total_output_tokens"] is None
    assert base["mean_input_tokens_per_task"] is None
    assert base["mean_output_tokens_per_task"] is None


def test_aggregate_emits_quality_metrics_section(
    runner: CliRunner, exp_dir: Path
) -> None:
    """``aggregate.json`` carries a per-task and overall quality view.

    Mixes a clean trial, an executor-level error, and a scorer-level error
    so the summary, per-config, and low-quality blocks all see traffic.
    """
    save_config_results(
        exp_dir,
        "baseline",
        [
            CompletedTask(
                task_id="task-001",
                automated_score=1.0,
                status="completed",
                duration_seconds=1.0,
                cost_usd=0.05,
                scoring_details={"passed": True, "error": None},
            ),
            CompletedTask(
                task_id="task-002",
                automated_score=0.0,
                status="error",
                error_category="timeout",
                duration_seconds=300.0,
                metadata={"error": "subprocess timed out"},
            ),
        ],
    )
    save_config_results(
        exp_dir,
        "variant",
        [
            CompletedTask(
                task_id="task-001",
                automated_score=0.0,
                status="completed",
                duration_seconds=1.0,
                scoring_details={
                    "passed": False,
                    "error": "tests/test.sh not found",
                },
            ),
            CompletedTask(
                task_id="task-002",
                automated_score=1.0,
                status="completed",
                duration_seconds=2.0,
                scoring_details={"passed": True, "error": None},
            ),
        ],
    )

    result = runner.invoke(main, ["experiment", "aggregate", str(exp_dir)])
    assert result.exit_code == 0, result.output

    report = json.loads((exp_dir / "reports" / "aggregate.json").read_text())
    assert "quality_metrics" in report

    qm = report["quality_metrics"]
    assert qm["schema_version"] == 1

    overall = qm["overall"]
    assert overall["total_trials"] == 4
    assert overall["invalid_trials"] == 1
    assert overall["valid_trials"] == 3
    # 1 timeout + 1 scorer_error.
    assert overall["low_quality_trials"] == 2
    assert overall["error_category_counts"].get("timeout") == 1
    assert overall["flag_counts"].get("invalid") == 1
    assert overall["flag_counts"].get("timeout") == 1
    assert overall["flag_counts"].get("scorer_error") == 1

    per_config = qm["per_config"]
    assert per_config["baseline"]["invalid_trials"] == 1
    assert per_config["variant"]["invalid_trials"] == 0

    low = qm["low_quality_trials"]
    by_id = {(row["task_id"], row["config_label"]): row for row in low}
    assert ("task-002", "baseline") in by_id
    assert by_id[("task-002", "baseline")]["validity_reason"] == "timeout"
    assert ("task-001", "variant") in by_id
    assert "scorer_error" in by_id[("task-001", "variant")]["quality_flags"]


def test_aggregate_no_results(runner: CliRunner, exp_dir: Path) -> None:
    """Aggregate with no results should still succeed (empty summaries)."""
    result = runner.invoke(main, ["experiment", "aggregate", str(exp_dir)])
    assert result.exit_code == 0


def test_aggregate_no_configs(runner: CliRunner, tmp_path: Path) -> None:
    exp = Experiment(name="nocfg", configs=[])
    d = create_experiment_dir(tmp_path, exp)
    result = runner.invoke(main, ["experiment", "aggregate", str(d)])
    assert result.exit_code == 1
    assert "at least 1 configuration" in result.output


# ---- repeats are first-class (codeprobe-f7rl.7) ----


def test_aggregate_pairwise_uses_per_task_means(
    runner: CliRunner, exp_dir: Path
) -> None:
    """Repeat trials of the same task must be averaged, not overwritten.

    baseline task-001 scores [1.0, 0.0] (per-task mean 0.5); variant
    task-001 scores [0.0, 0.0] (mean 0.0). The old task_id-keyed dict kept
    only the last repeat (0.0 vs 0.0 -> delta 0.0, tie); per-task means
    give mean_delta -0.5 with no ties.
    """
    save_config_results(
        exp_dir,
        "baseline",
        [
            CompletedTask(task_id="task-001", automated_score=1.0, repeat_index=0),
            CompletedTask(task_id="task-001", automated_score=0.0, repeat_index=1),
        ],
    )
    save_config_results(
        exp_dir,
        "variant",
        [
            CompletedTask(task_id="task-001", automated_score=0.0, repeat_index=0),
            CompletedTask(task_id="task-001", automated_score=0.0, repeat_index=1),
        ],
    )

    # repeat_index round-trips through results.json, so the aggregate rows
    # projection can carry it for disaggregation.
    from codeprobe.core.experiment import load_config_results

    loaded = load_config_results(exp_dir, "baseline")
    assert sorted(t.repeat_index for t in loaded.completed) == [0, 1]

    result = runner.invoke(main, ["experiment", "aggregate", str(exp_dir)])
    assert result.exit_code == 0, result.output

    report = json.loads((exp_dir / "reports" / "aggregate.json").read_text())
    pairwise = report["pairwise_deltas"]
    assert len(pairwise) == 1
    entry = pairwise[0]
    assert entry["config_a"] == "baseline"
    assert entry["config_b"] == "variant"
    assert entry["shared_tasks"] == 1
    assert entry["mean_delta"] == pytest.approx(-0.5)
    assert entry["ties"] == 0
    assert entry["wins_a"] == 1


def test_aggregate_disjoint_pair_emitted_not_comparable(
    runner: CliRunner, exp_dir: Path
) -> None:
    """Zero shared tasks -> explicit comparable:false entry, not omission."""
    save_config_results(
        exp_dir,
        "baseline",
        [CompletedTask(task_id="task-001", automated_score=1.0)],
    )
    save_config_results(
        exp_dir,
        "variant",
        [CompletedTask(task_id="task-002", automated_score=1.0)],
    )

    result = runner.invoke(main, ["experiment", "aggregate", str(exp_dir)])
    assert result.exit_code == 0, result.output

    report = json.loads((exp_dir / "reports" / "aggregate.json").read_text())
    pairwise = report["pairwise_deltas"]
    assert len(pairwise) == 1
    entry = pairwise[0]
    assert entry["config_a"] == "baseline"
    assert entry["config_b"] == "variant"
    assert entry["shared_tasks"] == 0
    assert entry["comparable"] is False


def test_status_completes_with_repeats(runner: CliRunner, tmp_path: Path) -> None:
    """A finished repeated run must show complete, not pending forever.

    1 task dir, 2 trials of the same task_id: completion is judged on
    distinct task_ids, with the trial count displayed alongside.
    """
    exp = Experiment(
        name="repeat-exp",
        configs=[ExperimentConfig(label="baseline", agent="claude")],
        tasks_dir="tasks",
    )
    d = create_experiment_dir(tmp_path, exp)
    task_dir = d / "tasks" / "task-001"
    task_dir.mkdir(parents=True)
    (task_dir / "instruction.md").write_text("# task-001\nDo something.\n")

    save_config_results(
        d,
        "baseline",
        [
            CompletedTask(task_id="task-001", automated_score=1.0, repeat_index=0),
            CompletedTask(task_id="task-001", automated_score=0.5, repeat_index=1),
        ],
    )

    result = runner.invoke(main, ["experiment", "status", str(d)])
    assert result.exit_code == 0, result.output
    assert "complete" in result.output
    assert "(2 trials)" in result.output
    assert "pending" not in result.output
# ---- repo-root path resolution (codeprobe-f7rl.13) ----
#
# Every path token accepted by ``experiment init`` must be accepted by
# status / add-config / validate / aggregate. Before the shared resolver
# these commands did ``load_experiment(Path(path))`` directly and rejected
# the repo root that ``experiment init . --non-interactive`` had just
# accepted with "Experiment not found: experiment.json".


def _init_non_interactive(runner: CliRunner, repo: Path) -> None:
    result = runner.invoke(
        main, ["experiment", "init", str(repo), "--non-interactive"]
    )
    assert result.exit_code == 0, result.output


def test_status_accepts_repo_root_after_init(
    runner: CliRunner, tmp_path: Path
) -> None:
    _init_non_interactive(runner, tmp_path)
    result = runner.invoke(main, ["experiment", "status", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Experiment: default" in result.output
    assert "Experiment not found" not in result.output


def test_add_config_accepts_repo_root_after_init(
    runner: CliRunner, tmp_path: Path
) -> None:
    _init_non_interactive(runner, tmp_path)
    result = runner.invoke(
        main,
        [
            "experiment",
            "add-config",
            str(tmp_path),
            "--label",
            "x",
            "--agent",
            "claude",
        ],
    )
    assert result.exit_code == 0, result.output
    saved = load_experiment(tmp_path / ".codeprobe")
    assert [c.label for c in saved.configs] == ["x"]
    assert (tmp_path / ".codeprobe" / "runs" / "x").is_dir()


def test_validate_accepts_repo_root_after_init(
    runner: CliRunner, tmp_path: Path
) -> None:
    _init_non_interactive(runner, tmp_path)
    result = runner.invoke(main, ["experiment", "validate", str(tmp_path)])
    # Fails on content (no tasks / no configs yet), not on resolution.
    assert result.exit_code == 1
    assert "Experiment not found" not in result.output
    assert "No tasks" in result.output


def test_aggregate_accepts_repo_root_after_init(
    runner: CliRunner, tmp_path: Path
) -> None:
    _init_non_interactive(runner, tmp_path)
    add = runner.invoke(
        main,
        ["experiment", "add-config", str(tmp_path), "--label", "x"],
    )
    assert add.exit_code == 0, add.output
    result = runner.invoke(main, ["experiment", "aggregate", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".codeprobe" / "reports" / "aggregate.json").is_file()


def test_status_accepts_explicit_codeprobe_dir(
    runner: CliRunner, tmp_path: Path
) -> None:
    _init_non_interactive(runner, tmp_path)
    result = runner.invoke(
        main, ["experiment", "status", str(tmp_path / ".codeprobe")]
    )
    assert result.exit_code == 0, result.output
    assert "Experiment: default" in result.output


def test_status_resolves_named_subdir_layout(
    runner: CliRunner, tmp_path: Path
) -> None:
    named = tmp_path / ".codeprobe" / "myexp"
    named.mkdir(parents=True)
    save_experiment(named, Experiment(name="myexp"))
    result = runner.invoke(main, ["experiment", "status", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Experiment: myexp" in result.output


@pytest.mark.parametrize(
    "argv",
    [
        ["experiment", "status"],
        ["experiment", "add-config", "--label", "x"],
        ["experiment", "validate"],
        ["experiment", "aggregate"],
    ],
    ids=["status", "add-config", "validate", "aggregate"],
)
def test_zero_experiment_reports_no_experiment(
    runner: CliRunner, tmp_path: Path, argv: list[str]
) -> None:
    """No experiment anywhere → NO_EXPERIMENT envelope, not the old
    unhelpful "Experiment not found: experiment.json" text."""
    full_argv = [argv[0], argv[1], str(tmp_path), *argv[2:]]
    result = runner.invoke(main, full_argv)
    assert result.exit_code == 2, result.output
    envelope = json.loads(result.output)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "NO_EXPERIMENT"
    combined = result.output + (result.stderr or "")
    assert "Experiment not found: experiment.json" not in combined
    assert "Traceback" not in combined
