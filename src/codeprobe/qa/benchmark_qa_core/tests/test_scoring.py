"""Tests for check_scoring_honesty."""

from __future__ import annotations

from codeprobe.qa.benchmark_qa_core import check_scoring_honesty

TIERS = {
    "exact_match": "strict",
    "fuzzy_match": "loose",
    "calibrated_llm": "calibrated",
    "experimental": "",
    "shaky": "unknown",
}


def codes(findings: list) -> list[str]:
    return [f.code for f in findings]


def test_clean_method_returns_no_findings() -> None:
    assert check_scoring_honesty({"scoring_method": "exact_match"}, TIERS) == []


def test_e1_missing_scoring_method() -> None:
    findings = check_scoring_honesty({}, TIERS)
    assert codes(findings) == ["E1"]
    assert findings[0].severity == "error"


def test_e2_unknown_scoring_method() -> None:
    findings = check_scoring_honesty({"scoring_method": "ad_hoc"}, TIERS)
    assert codes(findings) == ["E2"]
    assert findings[0].severity == "error"
    assert "ad_hoc" in findings[0].message


def test_e3_empty_tier_label_warns() -> None:
    findings = check_scoring_honesty({"scoring_method": "experimental"}, TIERS)
    assert codes(findings) == ["E3"]
    assert findings[0].severity == "warning"


def test_e3_unknown_sentinel_warns() -> None:
    findings = check_scoring_honesty({"scoring_method": "shaky"}, TIERS)
    assert codes(findings) == ["E3"]
    assert findings[0].severity == "warning"


def test_e1_short_circuits_e2() -> None:
    findings = check_scoring_honesty({}, TIERS)
    assert codes(findings) == ["E1"]


def test_empty_tier_table_with_missing_method() -> None:
    findings = check_scoring_honesty({}, {})
    assert codes(findings) == ["E1"]


def test_empty_tier_table_with_known_method_is_e2() -> None:
    findings = check_scoring_honesty({"scoring_method": "anything"}, {})
    assert codes(findings) == ["E2"]
