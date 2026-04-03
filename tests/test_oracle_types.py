"""Tests for count and boolean oracle types."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeprobe.mining.org_scale_oracle import extract_answer, oracle_check

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    tmp_path: Path,
    gt: dict,
    answer: str | None = None,
    name: str = "task1",
) -> Path:
    """Create a task dir with ground_truth.json and optional answer.txt."""
    task_dir = tmp_path / name
    task_dir.mkdir()
    (task_dir / "ground_truth.json").write_text(json.dumps(gt))
    if answer is not None:
        (task_dir / "answer.txt").write_text(answer)
    return task_dir


# ---------------------------------------------------------------------------
# Count oracle tests
# ---------------------------------------------------------------------------


class TestCountOracle:
    def test_exact_match(self, tmp_path: Path) -> None:
        td = _make_task(tmp_path, {"expected": 42, "oracle_type": "count"}, "42\n")
        result = oracle_check(td)
        assert result["score"] == 1.0
        assert result["agent_answer"] == 42
        assert result["error"] == ""

    def test_mismatch(self, tmp_path: Path) -> None:
        td = _make_task(tmp_path, {"expected": 42, "oracle_type": "count"}, "99\n")
        result = oracle_check(td)
        assert result["score"] == 0.0
        assert result["agent_answer"] == 99

    def test_tolerance_within(self, tmp_path: Path) -> None:
        td = _make_task(
            tmp_path,
            {"expected": 42, "oracle_type": "count", "tolerance": 2},
            "44\n",
        )
        result = oracle_check(td)
        assert result["score"] == 1.0
        assert result["tolerance"] == 2

    def test_tolerance_boundary(self, tmp_path: Path) -> None:
        """Exactly at tolerance boundary should pass."""
        td = _make_task(
            tmp_path,
            {"expected": 10, "oracle_type": "count", "tolerance": 3},
            "7\n",
        )
        result = oracle_check(td)
        assert result["score"] == 1.0

    def test_tolerance_exceeded(self, tmp_path: Path) -> None:
        td = _make_task(
            tmp_path,
            {"expected": 10, "oracle_type": "count", "tolerance": 2},
            "15\n",
        )
        result = oracle_check(td)
        assert result["score"] == 0.0

    def test_missing_answer(self, tmp_path: Path) -> None:
        td = _make_task(tmp_path, {"expected": 42, "oracle_type": "count"})
        result = oracle_check(td)
        assert result["score"] == 0.0
        assert "Empty or unparseable" in result["error"]

    def test_non_integer_answer(self, tmp_path: Path) -> None:
        td = _make_task(
            tmp_path, {"expected": 42, "oracle_type": "count"}, "not_a_number\n"
        )
        result = oracle_check(td)
        assert result["score"] == 0.0

    def test_skips_comments(self, tmp_path: Path) -> None:
        td = _make_task(
            tmp_path, {"expected": 5, "oracle_type": "count"}, "# comment\n5\n"
        )
        result = oracle_check(td)
        assert result["score"] == 1.0

    def test_extract_answer_returns_int(self, tmp_path: Path) -> None:
        td = _make_task(tmp_path, {}, "17\n")
        val = extract_answer(td, oracle_type="count")
        assert val == 17
        assert isinstance(val, int)


# ---------------------------------------------------------------------------
# Boolean oracle tests
# ---------------------------------------------------------------------------


class TestBooleanOracle:
    @pytest.mark.parametrize("answer_text", ["true", "True", "TRUE", "yes", "Yes", "1"])
    def test_true_variants_match_true(self, tmp_path: Path, answer_text: str) -> None:
        td = _make_task(
            tmp_path,
            {"expected": True, "oracle_type": "boolean"},
            f"{answer_text}\n",
            name=f"task_{answer_text}",
        )
        result = oracle_check(td)
        assert result["score"] == 1.0
        assert result["agent_answer"] is True

    @pytest.mark.parametrize(
        "answer_text", ["false", "False", "FALSE", "no", "No", "0"]
    )
    def test_false_variants_match_false(self, tmp_path: Path, answer_text: str) -> None:
        td = _make_task(
            tmp_path,
            {"expected": False, "oracle_type": "boolean"},
            f"{answer_text}\n",
            name=f"task_{answer_text}",
        )
        result = oracle_check(td)
        assert result["score"] == 1.0
        assert result["agent_answer"] is False

    def test_mismatch(self, tmp_path: Path) -> None:
        td = _make_task(
            tmp_path, {"expected": True, "oracle_type": "boolean"}, "false\n"
        )
        result = oracle_check(td)
        assert result["score"] == 0.0
        assert result["agent_answer"] is False

    def test_reverse_mismatch(self, tmp_path: Path) -> None:
        td = _make_task(
            tmp_path, {"expected": False, "oracle_type": "boolean"}, "yes\n"
        )
        result = oracle_check(td)
        assert result["score"] == 0.0

    def test_missing_answer(self, tmp_path: Path) -> None:
        td = _make_task(tmp_path, {"expected": True, "oracle_type": "boolean"})
        result = oracle_check(td)
        assert result["score"] == 0.0
        assert "Empty or unparseable" in result["error"]

    def test_unrecognized_value(self, tmp_path: Path) -> None:
        td = _make_task(
            tmp_path, {"expected": True, "oracle_type": "boolean"}, "maybe\n"
        )
        result = oracle_check(td)
        assert result["score"] == 0.0

    def test_skips_comments(self, tmp_path: Path) -> None:
        td = _make_task(
            tmp_path,
            {"expected": True, "oracle_type": "boolean"},
            "# note\ntrue\n",
        )
        result = oracle_check(td)
        assert result["score"] == 1.0

    def test_extract_answer_returns_bool(self, tmp_path: Path) -> None:
        td = _make_task(tmp_path, {}, "yes\n")
        val = extract_answer(td, oracle_type="boolean")
        assert val is True
        assert isinstance(val, bool)


# ---------------------------------------------------------------------------
# File-list backward compatibility
# ---------------------------------------------------------------------------


class TestFileListBackwardCompat:
    def test_no_oracle_type_defaults_to_file_list(self, tmp_path: Path) -> None:
        """ground_truth.json without oracle_type still works."""
        td = _make_task(
            tmp_path,
            {"expected": ["pkg/a.go", "pkg/b.go"]},
            "pkg/a.go\npkg/b.go\n",
        )
        result = oracle_check(td)
        assert result["score"] == 1.0
        assert result["f1"] == 1.0

    def test_explicit_file_list_type(self, tmp_path: Path) -> None:
        td = _make_task(
            tmp_path,
            {"expected": ["pkg/a.go"], "oracle_type": "file_list"},
            "pkg/a.go\n",
        )
        result = oracle_check(td)
        assert result["score"] == 1.0

    def test_extract_answer_default_returns_list(self, tmp_path: Path) -> None:
        td = _make_task(tmp_path, {}, "pkg/a.go\npkg/b.go\n")
        val = extract_answer(td)
        assert isinstance(val, list)
        assert len(val) == 2


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestOracleEdgeCases:
    def test_unknown_oracle_type(self, tmp_path: Path) -> None:
        td = _make_task(tmp_path, {"expected": "x", "oracle_type": "regex"}, "x\n")
        result = oracle_check(td)
        assert result["score"] == 0.0
        assert "Unknown oracle_type" in result["error"]

    def test_count_expected_not_int(self, tmp_path: Path) -> None:
        td = _make_task(
            tmp_path, {"expected": "forty-two", "oracle_type": "count"}, "42\n"
        )
        result = oracle_check(td)
        assert result["score"] == 0.0
        assert "not an int" in result["error"]

    def test_boolean_expected_not_bool(self, tmp_path: Path) -> None:
        td = _make_task(
            tmp_path, {"expected": "yes", "oracle_type": "boolean"}, "yes\n"
        )
        result = oracle_check(td)
        assert result["score"] == 0.0
        assert "not a bool" in result["error"]
