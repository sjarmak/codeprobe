"""Tests for Finding and OracleConstraints value semantics."""

from __future__ import annotations

import pytest

from codeprobe.qa.benchmark_qa_core import Finding, OracleConstraints


def test_finding_is_frozen() -> None:
    f = Finding(severity="error", code="A1", message="boom")
    with pytest.raises(Exception):
        f.severity = "warning"  # type: ignore[misc]


def test_finding_equality_uses_value_semantics() -> None:
    a = Finding(severity="error", code="A1", message="boom", location="x.py")
    b = Finding(severity="error", code="A1", message="boom", location="x.py")
    assert a == b
    assert hash(a) == hash(b)


def test_finding_default_optional_fields_are_none() -> None:
    f = Finding(severity="info", code="X1", message="hi")
    assert f.location is None
    assert f.suggested_fix is None


def test_oracle_constraints_defaults_are_empty() -> None:
    c = OracleConstraints()
    assert c.expected_languages == frozenset()
    assert c.path_include == ()
    assert c.path_exclude == ()
    assert c.require_symbols_resolve is True
    assert c.language_extensions is None


def test_oracle_constraints_is_frozen() -> None:
    c = OracleConstraints()
    with pytest.raises(Exception):
        c.path_include = ("x",)  # type: ignore[misc]
