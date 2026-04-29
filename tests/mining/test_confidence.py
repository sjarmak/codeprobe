"""Tests for mining.confidence — sub-scores, composite, threshold gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeprobe.mining.confidence import (
    DEFAULT_THRESHOLD,
    ConfidenceScore,
    confidence_histogram,
    load_confidence_file,
    score_task_confidence,
    score_tasks_dir,
    write_confidence_file,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


_GOOD_INSTRUCTION = (
    "# Find references to Foo\n\n"
    "**Repository:** demo\n\n"
    "## Problem\n\n"
    "Find all files that reference the symbol `Foo` defined in src/foo.py. "
    "Return them as a list of file paths in answer.txt.\n\n"
    "## Task\n\n"
    "Write the answer to answer.txt with one path per line.\n"
)


def _write_task(
    task_dir: Path,
    *,
    expected_files: list[str] | None = None,
    instruction: str = _GOOD_INSTRUCTION,
    verification_mode: str = "test_script",
    verification_type: str = "oracle",
) -> Path:
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "instruction.md").write_text(instruction, encoding="utf-8")

    metadata = {
        "id": task_dir.name,
        "metadata": {"category": "demo"},
        "verification": {
            "type": verification_type,
            "verification_mode": verification_mode,
            "oracle_type": "file_list",
            "oracle_answer": expected_files or [],
        },
    }
    (task_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )

    if expected_files is not None:
        gt = {
            "schema_version": 1,
            "oracle_type": "file_list",
            "expected": expected_files,
        }
        (task_dir / "ground_truth.json").write_text(
            json.dumps(gt, indent=2), encoding="utf-8"
        )
    return task_dir


# ---------------------------------------------------------------------------
# Sub-score: ground truth size
# ---------------------------------------------------------------------------


def test_size_score_zero_files_is_zero(tmp_path: Path) -> None:
    task = _write_task(tmp_path / "t", expected_files=[])
    s = score_task_confidence(task)
    assert s.breakdown["ground_truth_size"] == 0.0


def test_size_score_narrow_is_low(tmp_path: Path) -> None:
    task = _write_task(tmp_path / "t", expected_files=["a.py"])
    s = score_task_confidence(task)
    assert s.breakdown["ground_truth_size"] == 0.3


def test_size_score_sweet_spot(tmp_path: Path) -> None:
    task = _write_task(tmp_path / "t", expected_files=[f"a{i}.py" for i in range(10)])
    s = score_task_confidence(task)
    assert s.breakdown["ground_truth_size"] == 1.0


def test_size_score_too_large(tmp_path: Path) -> None:
    task = _write_task(
        tmp_path / "t", expected_files=[f"a{i}.py" for i in range(120)]
    )
    s = score_task_confidence(task)
    assert s.breakdown["ground_truth_size"] == 0.3


# ---------------------------------------------------------------------------
# Sub-score: instruction quality
# ---------------------------------------------------------------------------


def test_instruction_score_short_instruction(tmp_path: Path) -> None:
    task = _write_task(tmp_path / "t", instruction="too short", expected_files=["a.py"] * 5)
    s = score_task_confidence(task)
    assert s.breakdown["instruction_quality"] == 0.3


def test_instruction_score_truncated(tmp_path: Path) -> None:
    body = "# Title\n\n" + "A" * 200 + "\n\n[...truncated]\n## Task\n\nDo it.\n"
    task = _write_task(tmp_path / "t", instruction=body, expected_files=["a.py"] * 5)
    s = score_task_confidence(task)
    assert s.breakdown["instruction_quality"] == 0.6


def test_instruction_score_well_formed(tmp_path: Path) -> None:
    task = _write_task(tmp_path / "t", expected_files=["a.py"] * 5)
    s = score_task_confidence(task)
    assert s.breakdown["instruction_quality"] == 1.0


def test_instruction_score_no_section_header(tmp_path: Path) -> None:
    body = (
        "# Title\n\n"
        + "A long enough instruction body to exceed the 100-char minimum length "
        "requirement so it doesn't trip the thin-instruction branch — but no "
        "section header marker present.\n"
    )
    task = _write_task(tmp_path / "t", instruction=body, expected_files=["a.py"] * 5)
    s = score_task_confidence(task)
    assert s.breakdown["instruction_quality"] == 0.5


# ---------------------------------------------------------------------------
# Sub-score: verification mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("dual", 1.0),
        ("test_script", 0.8),
        ("oracle", 0.6),
        ("heuristic", 0.3),
        ("", 0.3),
    ],
)
def test_verification_mode_score(tmp_path: Path, mode: str, expected: float) -> None:
    task = _write_task(
        tmp_path / "t", expected_files=["a.py"] * 5, verification_mode=mode,
    )
    s = score_task_confidence(task)
    assert s.breakdown["verification_mode"] == expected


# ---------------------------------------------------------------------------
# Sub-score: cross-source agreement
# ---------------------------------------------------------------------------


def test_cross_source_no_report_is_neutral(tmp_path: Path) -> None:
    task = _write_task(tmp_path / "t", expected_files=["a.py"] * 5)
    s = score_task_confidence(task)
    assert s.breakdown["cross_source_agreement"] == 0.6


def test_cross_source_flagged_task_is_low(tmp_path: Path) -> None:
    task = _write_task(tmp_path / "t1", expected_files=["a.py"] * 5)
    report = {
        "summary": {"threshold": 0.6},
        "flagged_tasks": ["t1"],
        "per_task": [{"task_id": "t1", "min_f1": 0.0}],
    }
    s = score_task_confidence(task, cross_validation_report=report)
    assert s.breakdown["cross_source_agreement"] == 0.3


def test_cross_source_uses_min_f1(tmp_path: Path) -> None:
    task = _write_task(tmp_path / "t1", expected_files=["a.py"] * 5)
    report = {
        "summary": {"threshold": 0.6},
        "flagged_tasks": [],
        "per_task": [{"task_id": "t1", "min_f1": 0.85}],
    }
    s = score_task_confidence(task, cross_validation_report=report)
    assert s.breakdown["cross_source_agreement"] == 0.85


def test_cross_source_single_backend_is_neutral(tmp_path: Path) -> None:
    task = _write_task(tmp_path / "t1", expected_files=["a.py"] * 5)
    report = {
        "summary": {"threshold": 0.6},
        "flagged_tasks": [],
        "per_task": [{"task_id": "t1", "min_f1": None}],
    }
    s = score_task_confidence(task, cross_validation_report=report)
    assert s.breakdown["cross_source_agreement"] == 0.6


def test_cross_source_loads_report_from_disk(tmp_path: Path) -> None:
    """When a report exists at <tasks_dir>/.codeprobe/, load it transparently."""
    tasks_dir = tmp_path / "tasks"
    task = _write_task(tasks_dir / "t1", expected_files=["a.py"] * 5)
    report_dir = tasks_dir / ".codeprobe"
    report_dir.mkdir()
    report = {
        "flagged_tasks": ["t1"],
        "per_task": [{"task_id": "t1", "min_f1": 0.1}],
    }
    (report_dir / "cross_validation_report.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    s = score_task_confidence(task)
    # flagged → 0.3
    assert s.breakdown["cross_source_agreement"] == 0.3


# ---------------------------------------------------------------------------
# Composite + threshold gate
# ---------------------------------------------------------------------------


def test_composite_is_mean_of_subscores(tmp_path: Path) -> None:
    task = _write_task(
        tmp_path / "t",
        expected_files=["a.py"] * 5,  # size=1.0
        verification_mode="dual",  # verification=1.0
        # instruction=1.0, cross-source=0.6
    )
    s = score_task_confidence(task)
    expected = round((1.0 + 1.0 + 1.0 + 0.6) / 4, 4)
    assert s.score == expected


def test_promoted_when_above_threshold(tmp_path: Path) -> None:
    task = _write_task(
        tmp_path / "t",
        expected_files=["a.py"] * 5,
        verification_mode="dual",
    )
    s = score_task_confidence(task)
    assert s.promoted is True


def test_quarantined_when_below_threshold(tmp_path: Path) -> None:
    # Force a low score: empty GT (0.0) + thin instruction (0.3) +
    # heuristic verification (0.3) + neutral cross-source (0.6) → mean 0.3
    task = _write_task(
        tmp_path / "t",
        expected_files=[],
        instruction="too short",
        verification_mode="heuristic",
    )
    s = score_task_confidence(task)
    assert s.promoted is False
    assert s.score < DEFAULT_THRESHOLD


def test_invalid_threshold_raises(tmp_path: Path) -> None:
    task = _write_task(tmp_path / "t", expected_files=["a.py"])
    with pytest.raises(ValueError):
        score_task_confidence(task, threshold=1.5)


# ---------------------------------------------------------------------------
# File IO helpers
# ---------------------------------------------------------------------------


def test_write_and_load_confidence_file(tmp_path: Path) -> None:
    score = ConfidenceScore(
        task_id="abc",
        score=0.75,
        breakdown={"a": 0.5, "b": 1.0},
        notes={"a": "ok"},
    )
    out = write_confidence_file(score, tmp_path)
    loaded = load_confidence_file(tmp_path)
    assert loaded is not None
    assert loaded.task_id == "abc"
    assert loaded.score == 0.75
    assert loaded.breakdown == {"a": 0.5, "b": 1.0}
    assert out == tmp_path / "confidence.json"


def test_load_confidence_file_missing(tmp_path: Path) -> None:
    assert load_confidence_file(tmp_path) is None


def test_load_confidence_file_malformed(tmp_path: Path) -> None:
    (tmp_path / "confidence.json").write_text("not json", encoding="utf-8")
    assert load_confidence_file(tmp_path) is None


def test_score_tasks_dir_writes_each_confidence(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks"
    _write_task(tasks_dir / "t1", expected_files=["a.py"] * 5)
    _write_task(tasks_dir / "t2", expected_files=["a.py"] * 5)
    scores = score_tasks_dir(tasks_dir)
    assert {s.task_id for s in scores} == {"t1", "t2"}
    assert (tasks_dir / "t1" / "confidence.json").is_file()
    assert (tasks_dir / "t2" / "confidence.json").is_file()


# ---------------------------------------------------------------------------
# Histogram
# ---------------------------------------------------------------------------


def test_confidence_histogram_buckets() -> None:
    scores = [
        ConfidenceScore(task_id="a", score=0.1),
        ConfidenceScore(task_id="b", score=0.3),
        ConfidenceScore(task_id="c", score=0.45),
        ConfidenceScore(task_id="d", score=0.55),
        ConfidenceScore(task_id="e", score=0.8),
        ConfidenceScore(task_id="f", score=1.0),
    ]
    hist = confidence_histogram(scores)
    assert hist == {
        "0.0-0.2": 1,
        "0.2-0.4": 1,
        "0.4-0.5": 1,
        "0.5-0.7": 1,
        "0.7-0.9": 1,
        "0.9-1.0": 1,
    }


def test_confidence_histogram_empty() -> None:
    hist = confidence_histogram([])
    assert all(v == 0 for v in hist.values())
    assert list(hist) == [
        "0.0-0.2", "0.2-0.4", "0.4-0.5", "0.5-0.7", "0.7-0.9", "0.9-1.0",
    ]


# ---------------------------------------------------------------------------
# experiment validate gate (integration)
# ---------------------------------------------------------------------------


def _write_minimal_experiment(tmp_path: Path, *, low_confidence_task: bool) -> Path:
    """Create a minimal experiment dir for validate-gate integration testing."""
    exp = tmp_path / "exp"
    exp.mkdir(parents=True)

    experiment_payload = {
        "name": "test-exp",
        "description": "x",
        "created_at": "2026-04-28T00:00:00Z",
        "tasks_dir": "tasks",
        "configs": [
            {
                "label": "main",
                "model": "claude-sonnet-4-6",
                "agent": "claude-code",
                "permission_mode": "auto",
            }
        ],
    }
    (exp / "experiment.json").write_text(
        json.dumps(experiment_payload), encoding="utf-8"
    )

    tasks_dir = exp / "tasks"
    if low_confidence_task:
        # Will yield: empty GT + thin instruction + heuristic + neutral → ~0.3
        _write_task(
            tasks_dir / "t1",
            expected_files=[],
            instruction="too short",
            verification_mode="heuristic",
        )
    else:
        _write_task(
            tasks_dir / "t1",
            expected_files=["a.py"] * 5,
            verification_mode="dual",
        )
    (tasks_dir / "t1" / "tests").mkdir(exist_ok=True)
    (tasks_dir / "t1" / "tests" / "test.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    return exp


def test_experiment_validate_quarantines_low_confidence(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from codeprobe.cli import main

    exp = _write_minimal_experiment(tmp_path, low_confidence_task=True)
    runner = CliRunner()
    result = runner.invoke(main, ["experiment", "validate", str(exp)])
    assert result.exit_code == 1, result.output
    assert "quarantined" in result.output


def test_experiment_validate_admits_with_flag(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from codeprobe.cli import main

    exp = _write_minimal_experiment(tmp_path, low_confidence_task=True)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["experiment", "validate", str(exp), "--allow-low-confidence"],
    )
    # Still has a warning, but admitted → exit 0 (no errors)
    assert result.exit_code == 0, result.output
    assert "admitted via --allow-low-confidence" in result.output


def test_experiment_validate_passes_high_confidence(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from codeprobe.cli import main

    exp = _write_minimal_experiment(tmp_path, low_confidence_task=False)
    runner = CliRunner()
    result = runner.invoke(main, ["experiment", "validate", str(exp)])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# experiment status histogram
# ---------------------------------------------------------------------------


def test_experiment_status_shows_histogram(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from codeprobe.cli import main

    exp = _write_minimal_experiment(tmp_path, low_confidence_task=False)
    runner = CliRunner()
    result = runner.invoke(main, ["experiment", "status", str(exp)])
    assert result.exit_code == 0, result.output
    assert "Confidence histogram" in result.output
