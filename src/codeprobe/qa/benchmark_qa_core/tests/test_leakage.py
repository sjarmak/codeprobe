"""Tests for check_aux_file_leakage."""

from __future__ import annotations

from pathlib import Path

import pytest

from codeprobe.qa.benchmark_qa_core import check_aux_file_leakage


def codes(findings: list) -> list[str]:
    return [f.code for f in findings]


@pytest.fixture
def aux_dir(tmp_path: Path) -> Path:
    (tmp_path / "clean.txt").write_text("nothing to see here\n")
    (tmp_path / "leaky.md").write_text("the answer is compute_widget_score\n")
    (tmp_path / "subword.txt").write_text("not_compute_widget_score_extra\n")
    return tmp_path


def test_no_findings_when_aux_files_clean(aux_dir: Path) -> None:
    findings = check_aux_file_leakage(
        oracle_tokens=["compute_widget_score"],
        aux_files=[aux_dir / "clean.txt"],
    )
    assert findings == []


def test_f2_token_in_aux_file(aux_dir: Path) -> None:
    findings = check_aux_file_leakage(
        oracle_tokens=["compute_widget_score"],
        aux_files=[aux_dir / "leaky.md"],
    )
    assert codes(findings) == ["F2"]
    assert findings[0].severity == "error"
    assert findings[0].location == str(aux_dir / "leaky.md")


def test_word_boundary_avoids_subword_false_positive(aux_dir: Path) -> None:
    findings = check_aux_file_leakage(
        oracle_tokens=["compute_widget_score"],
        aux_files=[aux_dir / "subword.txt"],
    )
    assert findings == []


def test_f1_short_token_emits_info_and_skips_match(aux_dir: Path) -> None:
    (aux_dir / "shorty.txt").write_text("a b c\n")
    findings = check_aux_file_leakage(
        oracle_tokens=["a"],
        aux_files=[aux_dir / "shorty.txt"],
    )
    assert codes(findings) == ["F1"]
    assert findings[0].severity == "info"


def test_f3_missing_aux_file(tmp_path: Path) -> None:
    findings = check_aux_file_leakage(
        oracle_tokens=["compute_widget_score"],
        aux_files=[tmp_path / "does_not_exist.txt"],
    )
    assert codes(findings) == ["F3"]
    assert findings[0].severity == "warning"


def test_f3_emitted_only_once_per_missing_file(tmp_path: Path) -> None:
    findings = check_aux_file_leakage(
        oracle_tokens=["alpha_token", "beta_token", "gamma_token"],
        aux_files=[tmp_path / "missing.txt"],
    )
    assert codes(findings) == ["F3"]


def test_multiple_tokens_one_file(aux_dir: Path) -> None:
    (aux_dir / "double.md").write_text("alpha_token\nbeta_token\n")
    findings = check_aux_file_leakage(
        oracle_tokens=["alpha_token", "beta_token", "gamma_token"],
        aux_files=[aux_dir / "double.md"],
    )
    found_codes = codes(findings)
    assert found_codes.count("F2") == 2


def test_regex_special_chars_in_token_are_escaped(tmp_path: Path) -> None:
    f = tmp_path / "regex.txt"
    f.write_text("call func.method() here\n")
    findings = check_aux_file_leakage(
        oracle_tokens=["func.method"],
        aux_files=[f],
    )
    assert codes(findings) == ["F2"]


def test_token_major_file_minor_ordering(aux_dir: Path) -> None:
    (aux_dir / "first.md").write_text("alpha_token\n")
    (aux_dir / "second.md").write_text("beta_token\n")
    findings = check_aux_file_leakage(
        oracle_tokens=["alpha_token", "beta_token"],
        aux_files=[aux_dir / "first.md", aux_dir / "second.md"],
    )
    locations = [f.location for f in findings]
    assert locations == [
        str(aux_dir / "first.md"),
        str(aux_dir / "second.md"),
    ]
