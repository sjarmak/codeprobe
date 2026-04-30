"""Tests for aggregate-time bias detection.

Covers the three warning kinds emitted by ``core.bias_detection``:

* ``backend_overlap`` — config uses a backend that produced the GT.
* ``overshipping`` — loser at recall ≈1.0 hides a tool capability gap.
* ``no_independent_baseline`` — every task GT comes from one backend.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli import main
from codeprobe.core.bias_detection import (
    BiasWarning,
    collect_task_gt_backends,
    config_backends,
    detect_backend_overlap,
    detect_no_independent_baseline,
    detect_overshipping_anti_pattern,
    detect_task_gt_backends,
)
from codeprobe.core.experiment import (
    create_experiment_dir,
    save_config_results,
)
from codeprobe.models.experiment import (
    CompletedTask,
    Experiment,
    ExperimentConfig,
)


# ---- helpers ----


def _write_task(
    tasks_dir: Path,
    task_id: str,
    *,
    metadata_extras: dict | None = None,
    gt_extras: dict | None = None,
) -> Path:
    """Create a task directory with metadata.json + tests/ground_truth.json."""
    task_dir = tasks_dir / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "instruction.md").write_text(f"# {task_id}\n")
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test.sh").write_text("#!/bin/bash\nexit 0\n")

    metadata = {
        "id": task_id,
        "repo": "test-repo",
        "metadata": {
            "name": task_id,
            "category": "sdlc",
            "sg_repo": "",
            "mcp_capabilities_at_mine_time": [],
            **(metadata_extras or {}),
        },
    }
    (task_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )

    if gt_extras is not None:
        gt = {
            "schema_version": 2,
            "oracle_type": "file_list",
            "expected": [],
            "commit": "deadbeef",
            "pattern_used": metadata["metadata"]["category"],
            **gt_extras,
        }
        (tests_dir / "ground_truth.json").write_text(
            json.dumps(gt, indent=2), encoding="utf-8"
        )
    return task_dir


def _sg_mcp_config() -> dict:
    return {
        "mcpServers": {
            "sourcegraph": {
                "type": "http",
                "url": "https://sourcegraph.com/.api/mcp/v1",
                "headers": {"Authorization": "token X"},
            }
        }
    }


def _other_mcp_config() -> dict:
    return {
        "mcpServers": {
            "playwright": {"type": "http", "url": "https://example.com"},
        }
    }


# ---- detect_task_gt_backends ----


def test_gt_backend_from_curation_field(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    task_dir = _write_task(
        tasks_dir,
        "t1",
        gt_extras={
            "curation": {"backends_used": ["Grep", " sourcegraph "], "file_count": 4}
        },
    )
    backends = detect_task_gt_backends(task_dir)
    assert backends == frozenset({"grep", "sourcegraph"})


def test_gt_backend_from_sg_repo_and_mcp_category(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    task_dir = _write_task(
        tasks_dir,
        "t1",
        metadata_extras={
            "sg_repo": "github.com/sg-evals/codeprobe",
            "category": "symbol-reference-trace",
        },
    )
    backends = detect_task_gt_backends(task_dir)
    assert backends == frozenset({"sourcegraph"})


def test_gt_backend_empty_for_plain_sdlc(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    task_dir = _write_task(tasks_dir, "t1")
    assert detect_task_gt_backends(task_dir) == frozenset()


def test_gt_backend_from_csb_sidecar_explicit_consensus(tmp_path: Path) -> None:
    """CSB writes ``oracle_backends_consensus`` into ``ground_truth_meta.json``.

    Cross-rig consistency check (codeprobe-zf3k): bias detection must read
    that sidecar when ``ground_truth.json`` itself doesn't carry the field.
    """
    tasks_dir = tmp_path / "tasks"
    task_dir = _write_task(tasks_dir, "t1")
    sidecar = task_dir / "tests" / "ground_truth_meta.json"
    sidecar.write_text(
        json.dumps(
            {
                "has_ground_truth": True,
                "ground_truth_source": "curator_agent",
                "backend": "hybrid",
                "oracle_backends_consensus": ["local", "deepsearch"],
            }
        ),
        encoding="utf-8",
    )
    backends = detect_task_gt_backends(task_dir)
    assert backends == frozenset({"local", "deepsearch"})


def test_gt_backend_from_csb_sidecar_legacy_backend_string(tmp_path: Path) -> None:
    """Older CSB sidecars only carry ``backend: hybrid|local|deepsearch``.

    ``hybrid`` must expand to both underlying tools so a config that has
    only ``local`` (or only ``deepsearch``) is still flagged as overlapping.
    """
    tasks_dir = tmp_path / "tasks"
    task_dir = _write_task(tasks_dir, "t1")
    sidecar = task_dir / "tests" / "ground_truth_meta.json"
    sidecar.write_text(
        json.dumps(
            {
                "has_ground_truth": True,
                "ground_truth_source": "curator_agent",
                "backend": "hybrid",
            }
        ),
        encoding="utf-8",
    )
    assert detect_task_gt_backends(task_dir) == frozenset(
        {"local", "deepsearch"}
    )


def test_gt_backend_from_csb_sidecar_single_backend(tmp_path: Path) -> None:
    """Single-backend CSB curator runs (e.g. ``--backend local``) report one tool."""
    tasks_dir = tmp_path / "tasks"
    task_dir = _write_task(tasks_dir, "t1")
    sidecar = task_dir / "tests" / "ground_truth_meta.json"
    sidecar.write_text(
        json.dumps({"has_ground_truth": True, "backend": "local"}),
        encoding="utf-8",
    )
    assert detect_task_gt_backends(task_dir) == frozenset({"local"})


def test_gt_backend_unions_gt_and_sidecar(tmp_path: Path) -> None:
    """When both files declare backends, the result is their union."""
    tasks_dir = tmp_path / "tasks"
    task_dir = _write_task(
        tasks_dir,
        "t1",
        gt_extras={"oracle_backends_consensus": ["grep"]},
    )
    sidecar = task_dir / "tests" / "ground_truth_meta.json"
    sidecar.write_text(
        json.dumps({"oracle_backends_consensus": ["sourcegraph"]}),
        encoding="utf-8",
    )
    assert detect_task_gt_backends(task_dir) == frozenset(
        {"grep", "sourcegraph"}
    )


def test_gt_backend_sidecar_malformed_is_ignored(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    task_dir = _write_task(tasks_dir, "t1")
    sidecar = task_dir / "tests" / "ground_truth_meta.json"
    sidecar.write_text("not json", encoding="utf-8")
    assert detect_task_gt_backends(task_dir) == frozenset()


# ---- config_backends ----


def test_config_backends_extracts_lowercased_keys() -> None:
    cfg = ExperimentConfig(label="x", mcp_config=_sg_mcp_config())
    assert config_backends(cfg) == frozenset({"sourcegraph"})


def test_config_backends_empty_when_no_mcp() -> None:
    cfg = ExperimentConfig(label="baseline")
    assert config_backends(cfg) == frozenset()


# ---- detect_backend_overlap ----


def test_backend_overlap_flags_matching_config() -> None:
    configs = [
        ExperimentConfig(label="baseline"),
        ExperimentConfig(label="with-sg", mcp_config=_sg_mcp_config()),
    ]
    task_gt = {"t1": frozenset({"sourcegraph"})}
    warnings = detect_backend_overlap(configs, task_gt)
    assert len(warnings) == 1
    w = warnings[0]
    assert w.kind == "backend_overlap"
    assert w.detail["config"] == "with-sg"
    assert "sourcegraph" in w.detail["shared_backends"]
    assert w.detail["affected_task_count"] == 1


def test_backend_overlap_silent_when_no_overlap() -> None:
    configs = [
        ExperimentConfig(label="baseline"),
        ExperimentConfig(label="other", mcp_config=_other_mcp_config()),
    ]
    task_gt = {"t1": frozenset({"sourcegraph"})}
    assert detect_backend_overlap(configs, task_gt) == []


def test_backend_overlap_silent_when_no_gt_backends() -> None:
    configs = [ExperimentConfig(label="with-sg", mcp_config=_sg_mcp_config())]
    assert detect_backend_overlap(configs, {}) == []


# ---- backend_overlap severity (codeprobe-9re9) ----


def test_full_overlap_emits_warning_severity() -> None:
    """When the consensus is fully covered by the config, severity = warning.

    GT was produced by exactly the backend(s) the agent has access to; the
    score is potentially tautological. This is the historical ``backend_overlap``
    behavior — preserved at ``severity == "warning"``.
    """
    configs = [ExperimentConfig(label="with-sg", mcp_config=_sg_mcp_config())]
    task_gt = {"t1": frozenset({"sourcegraph"})}
    warnings = detect_backend_overlap(configs, task_gt)
    assert len(warnings) == 1
    w = warnings[0]
    assert w.kind == "backend_overlap"
    assert w.severity == "warning"
    assert w.detail["independent_backends"] == []
    # to_dict surfaces severity for downstream consumers (aggregate.json).
    assert w.to_dict()["severity"] == "warning"


def test_partial_overlap_with_independent_corroboration_is_informational() -> None:
    """When the consensus has backends the config can't reach, it's informational.

    Mirrors the codeprobe-9re9 motivating case: gascity ground truth is
    consensus = {ast, grep, sourcegraph}; the with-sourcegraph config has only
    ``sourcegraph`` in its MCP surface. ``ast`` and ``grep`` independently
    corroborate the answer key, so the with-sg score is not tautological —
    just an honest signal that the GT-producing backend is reachable.
    """
    configs = [ExperimentConfig(label="with-sg", mcp_config=_sg_mcp_config())]
    task_gt = {"t1": frozenset({"ast", "grep", "sourcegraph"})}
    warnings = detect_backend_overlap(configs, task_gt)
    assert len(warnings) == 1
    w = warnings[0]
    assert w.kind == "backend_overlap"
    assert w.severity == "informational"
    assert sorted(w.detail["independent_backends"]) == ["ast", "grep"]
    assert w.detail["shared_backends"] == ["sourcegraph"]
    # The informational message must mention independent corroboration so
    # readers understand why severity was downgraded.
    assert "informational" in w.message.lower() or "corroborated" in w.message.lower()
    assert w.to_dict()["severity"] == "informational"


def test_zero_overlap_emits_no_warning() -> None:
    """No shared backend → no warning at any severity.

    ``backend_overlap`` only fires when the agent's MCP surface intersects
    a GT-producing backend. A config exposing only Playwright vs. a GT
    consensus of {ast, grep, sourcegraph} has no overlap, so there is
    nothing to flag — severity logic is bypassed entirely.
    """
    configs = [
        ExperimentConfig(label="other", mcp_config=_other_mcp_config()),
    ]
    task_gt = {"t1": frozenset({"ast", "grep", "sourcegraph"})}
    assert detect_backend_overlap(configs, task_gt) == []


# ---- detect_overshipping_anti_pattern ----


def test_overshipping_flags_precision_gap_when_recall_matches() -> None:
    """Post-codeprobe-voxa: reward is recall, so over-shipping no longer
    suppresses the score. The warning is now informational — both configs
    score the same reward, but one shipped many more files than the other.
    """
    config_results = {
        "baseline": [
            {
                "task_id": "t1",
                "automated_score": 1.0,  # recall=1.0 is the new reward
                "scoring_details": {"recall": 1.0, "precision": 0.05},
            }
        ],
        "with-sg": [
            {
                "task_id": "t1",
                "automated_score": 1.0,
                "scoring_details": {"recall": 1.0, "precision": 1.0},
            }
        ],
    }
    warnings = detect_overshipping_anti_pattern(config_results)
    assert len(warnings) == 1
    w = warnings[0]
    assert w.kind == "overshipping"
    assert w.detail["over_shipper_config"] == "baseline"
    assert w.detail["tight_config"] == "with-sg"
    assert w.detail["over_shipper_precision"] == pytest.approx(0.05)
    assert w.detail["tight_precision"] == pytest.approx(1.0)
    assert w.detail["over_shipper_recall"] == pytest.approx(1.0)
    assert "over-shipped" in w.message
    assert "Reward is unaffected" in w.message


def test_overshipping_silent_when_loser_recall_low() -> None:
    """Recall < threshold on either side → not the over-ship pattern."""
    config_results = {
        "baseline": [
            {
                "task_id": "t1",
                "automated_score": 0.3,
                "scoring_details": {"recall": 0.3, "precision": 0.0},
            }
        ],
        "with-sg": [
            {
                "task_id": "t1",
                "automated_score": 1.0,
                "scoring_details": {"recall": 1.0, "precision": 1.0},
            },
        ],
    }
    assert detect_overshipping_anti_pattern(config_results) == []


def test_overshipping_silent_when_precision_gap_small() -> None:
    """Small precision delta → not flagged (configs behave similarly)."""
    config_results = {
        "baseline": [
            {
                "task_id": "t1",
                "automated_score": 1.0,
                "scoring_details": {"recall": 1.0, "precision": 0.6},
            }
        ],
        "with-sg": [
            {
                "task_id": "t1",
                "automated_score": 1.0,
                "scoring_details": {"recall": 1.0, "precision": 0.7},
            }
        ],
    }
    assert detect_overshipping_anti_pattern(config_results) == []


# ---- detect_no_independent_baseline ----


def test_no_independent_baseline_triggers_when_one_backend_dominates() -> None:
    configs = [
        ExperimentConfig(label="baseline"),
        ExperimentConfig(label="with-sg", mcp_config=_sg_mcp_config()),
    ]
    task_gt = {
        "t1": frozenset({"sourcegraph"}),
        "t2": frozenset({"sourcegraph"}),
    }
    warnings = detect_no_independent_baseline(configs, task_gt)
    assert len(warnings) == 1
    w = warnings[0]
    assert w.kind == "no_independent_baseline"
    assert w.detail["sole_backend"] == "sourcegraph"
    assert w.detail["configs_with_backend"] == ["with-sg"]
    assert w.detail["configs_without_backend"] == ["baseline"]


def test_no_independent_baseline_silent_when_all_configs_match() -> None:
    configs = [
        ExperimentConfig(label="a", mcp_config=_sg_mcp_config()),
        ExperimentConfig(label="b", mcp_config=_sg_mcp_config()),
    ]
    task_gt = {"t1": frozenset({"sourcegraph"})}
    assert detect_no_independent_baseline(configs, task_gt) == []


def test_no_independent_baseline_silent_when_no_config_matches() -> None:
    configs = [
        ExperimentConfig(label="baseline"),
        ExperimentConfig(label="other", mcp_config=_other_mcp_config()),
    ]
    task_gt = {"t1": frozenset({"sourcegraph"})}
    assert detect_no_independent_baseline(configs, task_gt) == []


def test_no_independent_baseline_silent_with_mixed_backends() -> None:
    configs = [
        ExperimentConfig(label="baseline"),
        ExperimentConfig(label="with-sg", mcp_config=_sg_mcp_config()),
    ]
    task_gt = {
        "t1": frozenset({"sourcegraph"}),
        "t2": frozenset({"grep"}),
    }
    assert detect_no_independent_baseline(configs, task_gt) == []


# ---- collect_task_gt_backends ----


def test_collect_task_gt_backends_walks_tasks_dir(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(
        tasks_dir,
        "t1",
        gt_extras={"curation": {"backends_used": ["sourcegraph"], "file_count": 1}},
    )
    _write_task(tasks_dir, "t2")  # no GT signal
    out = collect_task_gt_backends(tmp_path)
    assert out == {"t1": frozenset({"sourcegraph"})}


# ---- cross-rig consistency (codeprobe-zf3k) ----------------------------


def test_cross_rig_csb_artifact_round_trip(tmp_path: Path) -> None:
    """A CSB-shaped curator artifact is read losslessly by codeprobe.

    Acceptance for bead codeprobe-zf3k: codeprobe ingests downstream-rig
    artifacts without losing curator metadata. We simulate the exact
    files CodeScaleBench's ``write_curator_outputs`` produces (a thin
    ``ground_truth.json`` plus a ``ground_truth_meta.json`` sidecar
    carrying the consensus list) and assert codeprobe surfaces both
    backends.
    """
    tasks_dir = tmp_path / "tasks"
    task_dir = _write_task(tasks_dir, "csb-hybrid")
    sidecar = task_dir / "tests" / "ground_truth_meta.json"
    # Mirror the literal shape emitted by CodeScaleBench
    # scripts/running/context_retrieval_agent.py::write_curator_outputs
    # for a hybrid-backend run.
    sidecar.write_text(
        json.dumps(
            {
                "has_ground_truth": True,
                "has_chunk_ground_truth": False,
                "ground_truth_source": "curator_agent",
                "ground_truth_confidence": "medium",
                "task_name": "csb-hybrid",
                "curator_agent_version": "2.0",
                "model": "claude-opus-4-6",
                "backend": "hybrid",
                "oracle_backends_consensus": ["deepsearch", "local"],
                "timestamp": "2026-04-29T00:00:00Z",
                "files_count": 11,
                "edit_files_count": 0,
                "chunks_count": 0,
                "symbols_count": 14,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    backends = detect_task_gt_backends(task_dir)
    assert backends == frozenset({"deepsearch", "local"})


def test_cross_rig_eb_artifact_round_trip(tmp_path: Path) -> None:
    """An EnterpriseBench-shaped GT round-trips through codeprobe.

    The shim added in EnterpriseBench/scripts/validation/emit_oracle_consensus.py
    writes ``oracle_backends_consensus`` derived from per-file ``source``.
    Codeprobe must read the resulting field even though the rest of the
    GT shape is EB-specific (``required_files``, ``sufficient_files`` with
    ``repo`` keys etc.).
    """
    tasks_dir = tmp_path / "tasks"
    task_dir = _write_task(tasks_dir, "eb-task")
    gt = {
        "task_id": "eb-task",
        "task_type": "db_schema_evolution",
        "repos": [{"url": "https://github.com/example/x", "rev": "v1", "path": "x"}],
        "required_files": [
            {
                "path": "x/db/models/fields/__init__.py",
                "repo": "x",
                "confidence": 0.95,
                "source": "deterministic",
            }
        ],
        "sufficient_files": [
            {
                "path": "x/db/models/options.py",
                "repo": "x",
                "confidence": 0.85,
                "source": "both",
            }
        ],
        "oracle_backends_consensus": ["curator", "deterministic"],
    }
    (task_dir / "tests" / "ground_truth.json").write_text(
        json.dumps(gt, indent=2), encoding="utf-8"
    )
    backends = detect_task_gt_backends(task_dir)
    assert backends == frozenset({"curator", "deterministic"})


def test_cross_rig_csb_does_not_trigger_overlap_against_unrelated_config(
    tmp_path: Path,
) -> None:
    """Reading CSB provenance must not falsely flag unrelated MCP configs.

    A CSB ``deepsearch``-backend task should *not* trigger a backend overlap
    warning against a config that exposes only Playwright. The reader has
    to surface the consensus list correctly so the existing matcher logic
    sees no overlap.
    """
    tasks_dir = tmp_path / "tasks"
    task_dir = _write_task(tasks_dir, "csb-deep")
    sidecar = task_dir / "tests" / "ground_truth_meta.json"
    sidecar.write_text(
        json.dumps({"backend": "deepsearch"}),
        encoding="utf-8",
    )
    cfg = ExperimentConfig(label="playwright-only", mcp_config=_other_mcp_config())
    task_gt = collect_task_gt_backends(tmp_path)
    assert task_gt["csb-deep"] == frozenset({"deepsearch"})
    warnings = detect_backend_overlap([cfg], task_gt)
    assert warnings == []


# ---- end-to-end CLI ----


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _make_overlap_experiment(tmp_path: Path) -> Path:
    exp = Experiment(
        name="overlap-exp",
        configs=[
            ExperimentConfig(label="baseline"),
            ExperimentConfig(label="with-sg", mcp_config=_sg_mcp_config()),
        ],
    )
    d = create_experiment_dir(tmp_path, exp)
    tasks_dir = d / "tasks"
    _write_task(
        tasks_dir,
        "task-001",
        metadata_extras={
            "sg_repo": "github.com/example/repo",
            "category": "symbol-reference-trace",
        },
        gt_extras={
            "curation": {"backends_used": ["sourcegraph"], "file_count": 4}
        },
    )
    for label, score in (("baseline", 0.0), ("with-sg", 1.0)):
        save_config_results(
            d,
            label,
            [
                CompletedTask(
                    task_id="task-001",
                    automated_score=score,
                    duration_seconds=1.0,
                    cost_usd=0.05,
                    scoring_details={"recall": 1.0, "precision": score},
                )
            ],
        )
    return d


def test_aggregate_emits_warnings_and_json_records(
    runner: CliRunner, tmp_path: Path
) -> None:
    d = _make_overlap_experiment(tmp_path)
    result = runner.invoke(main, ["experiment", "aggregate", str(d)])
    assert result.exit_code == 0, result.output
    assert "Bias warnings:" in result.output
    assert "[backend_overlap]" in result.output
    assert "[overshipping]" in result.output
    assert "[no_independent_baseline]" in result.output
    # Winner suppression hides pairwise comparisons.
    assert "Pairwise Comparisons:" not in result.output
    assert "Pairwise comparisons suppressed" in result.output

    report = json.loads((d / "reports" / "aggregate.json").read_text())
    kinds = {w["kind"] for w in report["bias_warnings"]}
    assert kinds == {"backend_overlap", "overshipping", "no_independent_baseline"}


def test_aggregate_no_warn_silences_stdout(runner: CliRunner, tmp_path: Path) -> None:
    d = _make_overlap_experiment(tmp_path)
    result = runner.invoke(
        main, ["experiment", "aggregate", str(d), "--no-warn"]
    )
    assert result.exit_code == 0, result.output
    assert "Bias warnings:" not in result.output
    # With --no-warn winner suppression is disabled — pairwise should print.
    assert "Pairwise Comparisons:" in result.output

    # Structured warnings are still in aggregate.json regardless of --no-warn.
    report = json.loads((d / "reports" / "aggregate.json").read_text())
    assert len(report["bias_warnings"]) >= 1


def test_aggregate_clean_experiment_has_no_warnings(
    runner: CliRunner, tmp_path: Path
) -> None:
    """An SDLC-style experiment without GT-backend signals → no warnings."""
    exp = Experiment(
        name="clean",
        configs=[
            ExperimentConfig(label="baseline"),
            ExperimentConfig(label="variant", model="claude-sonnet-4-6"),
        ],
    )
    d = create_experiment_dir(tmp_path, exp)
    _write_task(d / "tasks", "task-001")
    save_config_results(
        d,
        "baseline",
        [CompletedTask(task_id="task-001", automated_score=0.5)],
    )
    save_config_results(
        d,
        "variant",
        [CompletedTask(task_id="task-001", automated_score=0.6)],
    )

    result = runner.invoke(main, ["experiment", "aggregate", str(d)])
    assert result.exit_code == 0, result.output
    assert "Bias warnings:" not in result.output
    report = json.loads((d / "reports" / "aggregate.json").read_text())
    assert report["bias_warnings"] == []


# Silence unused import lint — exposed for callers.
_ = BiasWarning
