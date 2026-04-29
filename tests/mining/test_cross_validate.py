"""Tests for mining.cross_validate — synthetic GT files with known divergences."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.mining.cross_validate import (
    BackendFile,
    cohens_kappa,
    compute_pair_metrics,
    cross_validate,
    discover_backend_files,
    discover_tasks,
    extract_file_set,
    write_report,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_gt(
    path: Path,
    files: list[str],
    *,
    oracle_type: str = "file_list",
    repo: str = "",
    pattern: str = "symbol-reference-trace",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "schema_version": 1,
        "oracle_type": oracle_type,
        "expected": files,
        "pattern_used": pattern,
    }
    if repo:
        payload["repo"] = repo
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def tasks_dir(tmp_path: Path) -> Path:
    """Build a synthetic tasks directory with three tasks.

    - task-agree:   sg + grep produce identical sets (F1 = 1.0)
    - task-partial: sg has 4 files, grep has 3 with 2 overlap (F1 ~0.57)
    - task-divergent: sg + grep disagree completely (F1 = 0.0)
    Plus one task with only a single backend (single).
    """
    root = tmp_path / "tasks"
    root.mkdir()

    # task-agree (perfect overlap)
    agree = root / "task-agree"
    _write_gt(agree / "ground_truth.json", ["a.py", "b.py", "c.py"])
    _write_gt(agree / "ground_truth_grep.json", ["a.py", "b.py", "c.py"])

    # task-partial — moderate divergence below 0.6 threshold
    partial = root / "task-partial"
    _write_gt(partial / "ground_truth.json", ["a.py", "b.py", "c.py", "d.py"])
    _write_gt(partial / "ground_truth_grep.json", ["a.py", "b.py", "x.py"])

    # task-divergent — zero overlap
    divergent = root / "task-divergent"
    _write_gt(divergent / "ground_truth.json", ["a.py", "b.py"])
    _write_gt(divergent / "ground_truth_grep.json", ["x.py", "y.py"])

    # task-single — only one backend, should be skipped from comparison
    single = root / "task-single"
    _write_gt(single / "ground_truth.json", ["a.py"])

    return root


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discover_backend_files_top_level(tmp_path: Path) -> None:
    task_dir = tmp_path / "task1"
    _write_gt(task_dir / "ground_truth.json", ["a.py"])
    _write_gt(task_dir / "ground_truth_ast.json", ["a.py"])
    _write_gt(task_dir / "ground_truth_grep.json", ["a.py"])

    found = discover_backend_files(task_dir)
    assert [b.backend for b in found] == ["ast", "default", "grep"]
    assert all(isinstance(b, BackendFile) for b in found)


def test_discover_backend_files_dual_layout(tmp_path: Path) -> None:
    """Dual/SDLC layout puts ground_truth.json under tests/."""
    task_dir = tmp_path / "task1"
    _write_gt(task_dir / "tests" / "ground_truth.json", ["a.py"])
    _write_gt(task_dir / "tests" / "ground_truth_sg.json", ["a.py"])

    found = discover_backend_files(task_dir)
    backends = sorted(b.backend for b in found)
    assert backends == ["default", "sg"]


def test_discover_backend_files_top_level_overrides_tests(tmp_path: Path) -> None:
    """When both layouts ship a default ground_truth.json the top-level wins."""
    task_dir = tmp_path / "task1"
    _write_gt(task_dir / "ground_truth.json", ["top.py"])
    _write_gt(task_dir / "tests" / "ground_truth.json", ["nested.py"])

    found = discover_backend_files(task_dir)
    default = next(b for b in found if b.backend == "default")
    assert default.path.parent == task_dir


def test_discover_tasks_walks_one_level(tmp_path: Path) -> None:
    """Suite-grouped layouts (tasks/<suite>/<task>/) should also be found."""
    root = tmp_path / "tasks"
    suite = root / "csb_org_scale"
    _write_gt(suite / "task1" / "ground_truth.json", ["a.py"])
    _write_gt(suite / "task1" / "ground_truth_sg.json", ["a.py"])
    # Direct-child task layout
    _write_gt(root / "task2" / "ground_truth.json", ["b.py"])

    tasks = discover_tasks(root)
    names = sorted(p.name for p in tasks)
    assert names == ["task1", "task2"]


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def test_extract_file_set_normalizes_paths() -> None:
    data = {
        "oracle_type": "file_list",
        "expected": ["./a/b.py", "/workspace/c.py", "myrepo/d.py"],
        "repo": "myrepo",
    }
    files = extract_file_set(data)
    assert files == frozenset({"a/b.py", "c.py", "d.py"})


def test_extract_file_set_skips_non_filelist_oracles() -> None:
    assert extract_file_set({"oracle_type": "count", "expected": 5}) == frozenset()
    assert extract_file_set({"oracle_type": "boolean", "expected": True}) == frozenset()


def test_extract_file_set_handles_missing_fields() -> None:
    assert extract_file_set({}) == frozenset()
    assert extract_file_set({"oracle_type": "file_list"}) == frozenset()
    assert extract_file_set({"oracle_type": "file_list", "expected": "not a list"}) == frozenset()


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_compute_pair_metrics_perfect_overlap() -> None:
    a = frozenset({"x.py", "y.py"})
    b = frozenset({"x.py", "y.py"})
    m = compute_pair_metrics(a, b)
    assert m["f1"] == 1.0
    assert m["precision"] == 1.0
    assert m["recall"] == 1.0
    assert m["n_overlap"] == 2


def test_compute_pair_metrics_partial_overlap() -> None:
    a = frozenset({"a.py", "b.py", "c.py", "d.py"})
    b = frozenset({"a.py", "b.py", "x.py"})
    m = compute_pair_metrics(a, b)
    # tp=2, prec=2/3, recall=2/4=0.5, f1 = 2*0.667*0.5 / (1.167) ≈ 0.5714
    assert abs(float(m["f1"]) - 0.5714) < 0.001
    assert m["n_overlap"] == 2


def test_compute_pair_metrics_disjoint() -> None:
    a = frozenset({"a.py"})
    b = frozenset({"x.py"})
    m = compute_pair_metrics(a, b)
    assert m["f1"] == 0.0


def test_compute_pair_metrics_both_empty() -> None:
    m = compute_pair_metrics(frozenset(), frozenset())
    assert m["f1"] == 1.0  # vacuous agreement


def test_cohens_kappa_perfect_agreement() -> None:
    a = [frozenset({"x", "y"}), frozenset({"z"})]
    b = [frozenset({"x", "y"}), frozenset({"z"})]
    universe = frozenset({"x", "y", "z"})
    assert cohens_kappa(a, b, universe) == 1.0


def test_cohens_kappa_total_disagreement() -> None:
    a = [frozenset({"x"}), frozenset({"y"})]
    b = [frozenset({"y"}), frozenset({"x"})]
    universe = frozenset({"x", "y"})
    # Each rater agrees half by chance — kappa should be < 0.5
    k = cohens_kappa(a, b, universe)
    assert k <= 0.0


def test_cohens_kappa_empty_universe() -> None:
    assert cohens_kappa([frozenset()], [frozenset()], frozenset()) == 1.0


def test_cohens_kappa_mismatched_raters_raises() -> None:
    with pytest.raises(ValueError):
        cohens_kappa(
            [frozenset({"a"})],
            [frozenset({"a"}), frozenset({"b"})],
            frozenset({"a", "b"}),
        )


# ---------------------------------------------------------------------------
# Full run
# ---------------------------------------------------------------------------


def test_cross_validate_full_report(tasks_dir: Path) -> None:
    report = cross_validate(tasks_dir, threshold=0.6)
    summary = report["summary"]

    assert summary["total_tasks"] == 4
    assert summary["tasks_compared"] == 3
    assert summary["tasks_with_single_backend"] == 1
    # task-partial (~0.57) and task-divergent (0.0) are below 0.6
    assert summary["tasks_below_threshold"] == 2
    assert summary["tasks_above_threshold"] == 1

    flagged = set(report["flagged_tasks"])
    assert flagged == {"task-partial", "task-divergent"}

    # Pair summary should aggregate across the three compared tasks
    assert len(report["pair_summary"]) == 1
    pair = report["pair_summary"][0]
    assert pair["backend_a"] == "default"
    assert pair["backend_b"] == "grep"
    assert pair["n_tasks"] == 3
    assert pair["n_below_threshold"] == 2


def test_cross_validate_invalid_threshold_raises(tasks_dir: Path) -> None:
    with pytest.raises(ValueError):
        cross_validate(tasks_dir, threshold=1.5)


def test_cross_validate_per_family(tasks_dir: Path) -> None:
    report = cross_validate(tasks_dir, threshold=0.6)
    families = report["per_family"]
    # All three compared tasks share pattern_used="symbol-reference-trace"
    assert "symbol-reference-trace" in families
    assert families["symbol-reference-trace"]["n_tasks"] == 3
    assert families["symbol-reference-trace"]["n_below_threshold"] == 2


def test_write_report_creates_codeprobe_dir(tasks_dir: Path) -> None:
    report = cross_validate(tasks_dir, threshold=0.6)
    out = write_report(report, tasks_dir)
    assert out == tasks_dir / ".codeprobe" / "cross_validation_report.json"
    parsed = json.loads(out.read_text(encoding="utf-8"))
    assert parsed["summary"]["total_tasks"] == 4


def test_cross_validate_skips_non_filelist_oracle(tmp_path: Path) -> None:
    """When backend variants use different oracle_types, only file_list pairs compare."""
    root = tmp_path / "tasks"
    task = root / "task1"
    _write_gt(task / "ground_truth.json", ["a.py"])
    _write_gt(task / "ground_truth_count.json", [], oracle_type="count")

    report = cross_validate(root, threshold=0.6)
    # Only one usable backend remains — counts as single-backend, no comparison
    assert report["summary"]["tasks_compared"] == 0
    assert report["summary"]["tasks_with_single_backend"] == 1
    entry = report["per_task"][0]
    assert "count" in entry["skipped"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_mine_cross_validate_exit_code_when_below(tasks_dir: Path) -> None:
    from codeprobe.cli import main

    runner = CliRunner()
    result = runner.invoke(
        main, ["mine-cross-validate", "--no-json", str(tasks_dir)]
    )
    # task-partial + task-divergent below threshold → exit 1
    assert result.exit_code == 1, result.output
    assert "Below threshold: 2" in result.output


def test_cli_mine_cross_validate_exit_code_when_clean(tmp_path: Path) -> None:
    """No tasks below threshold should give exit 0."""
    from codeprobe.cli import main

    root = tmp_path / "tasks"
    task = root / "task-good"
    _write_gt(task / "ground_truth.json", ["a.py", "b.py"])
    _write_gt(task / "ground_truth_grep.json", ["a.py", "b.py"])

    runner = CliRunner()
    result = runner.invoke(main, ["mine-cross-validate", "--no-json", str(root)])
    assert result.exit_code == 0, result.output
    assert "Below threshold: 0" in result.output


def test_cli_mine_cross_validate_writes_json_report(tasks_dir: Path) -> None:
    from codeprobe.cli import main

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["mine-cross-validate", "--no-json", str(tasks_dir), "--threshold", "0.6"],
    )
    # exit 1 expected (below threshold), but the report should be written
    assert result.exit_code == 1
    report_path = tasks_dir / ".codeprobe" / "cross_validation_report.json"
    assert report_path.is_file()
    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert data["summary"]["threshold"] == 0.6


def test_cli_mine_cross_validate_threshold_flag(tasks_dir: Path) -> None:
    """Lowering the threshold should reduce flagged tasks."""
    from codeprobe.cli import main

    runner = CliRunner()
    # threshold=0.0 → strict less-than → no task can fall below → exit 0
    result = runner.invoke(
        main,
        ["mine-cross-validate", "--no-json", str(tasks_dir), "--threshold", "0.0"],
    )
    assert result.exit_code == 0, result.output


def test_cli_mine_cross_validate_json_output(tasks_dir: Path) -> None:
    from codeprobe.cli import main

    runner = CliRunner()
    result = runner.invoke(
        main, ["mine-cross-validate", "--json", str(tasks_dir)]
    )
    assert result.exit_code == 1
    # Last line should be a JSON envelope with command=mine-cross-validate
    last_line = next(
        line for line in reversed(result.output.splitlines()) if line.strip()
    )
    payload = json.loads(last_line)
    assert payload.get("command") == "mine-cross-validate"
    assert "report" in payload["data"]
