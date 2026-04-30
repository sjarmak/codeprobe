"""Tests for check_oracle_coherence."""

from __future__ import annotations

from pathlib import Path

import pytest

from codeprobe.qa.benchmark_qa_core import (
    OracleConstraints,
    check_oracle_coherence,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Build a tiny fake repo:

      src/foo.py — defines `bar`
      src/baz.ts — empty TypeScript file
      docs/README.md — Markdown
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("def bar():\n    return 1\n")
    (tmp_path / "src" / "baz.ts").write_text("")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "README.md").write_text("hello\n")
    return tmp_path


def codes(findings: list) -> list[str]:
    return [f.code for f in findings]


def test_clean_oracle_returns_no_findings(repo: Path) -> None:
    findings = check_oracle_coherence(
        instruction_text="touch foo.py",
        oracle_files=["src/foo.py"],
        oracle_symbols=[("src/foo.py", "bar")],
        repo_root=repo,
        constraints=OracleConstraints(
            expected_languages=frozenset({"python"}),
            path_include=("src/*",),
        ),
    )
    assert findings == []


def test_a1_missing_oracle_file(repo: Path) -> None:
    findings = check_oracle_coherence(
        instruction_text="",
        oracle_files=["src/missing.py"],
        oracle_symbols=[],
        repo_root=repo,
        constraints=OracleConstraints(),
    )
    assert codes(findings) == ["A1"]
    assert findings[0].severity == "error"
    assert findings[0].location == "src/missing.py"


def test_b1_missing_symbol_is_error_when_required(repo: Path) -> None:
    findings = check_oracle_coherence(
        instruction_text="",
        oracle_files=[],
        oracle_symbols=[("src/foo.py", "definitely_not_there")],
        repo_root=repo,
        constraints=OracleConstraints(require_symbols_resolve=True),
    )
    assert codes(findings) == ["B1"]
    assert findings[0].severity == "error"
    assert findings[0].location == "src/foo.py::definitely_not_there"


def test_b1_downgrades_to_warning_when_not_required(repo: Path) -> None:
    findings = check_oracle_coherence(
        instruction_text="",
        oracle_files=[],
        oracle_symbols=[("src/foo.py", "definitely_not_there")],
        repo_root=repo,
        constraints=OracleConstraints(require_symbols_resolve=False),
    )
    assert codes(findings) == ["B1"]
    assert findings[0].severity == "warning"


def test_b2_symbol_in_missing_file(repo: Path) -> None:
    findings = check_oracle_coherence(
        instruction_text="",
        oracle_files=[],
        oracle_symbols=[("src/missing.py", "anything")],
        repo_root=repo,
        constraints=OracleConstraints(),
    )
    assert codes(findings) == ["B2"]
    assert findings[0].location == "src/missing.py::anything"


def test_c1_language_mismatch(repo: Path) -> None:
    findings = check_oracle_coherence(
        instruction_text="",
        oracle_files=["src/baz.ts"],
        oracle_symbols=[],
        repo_root=repo,
        constraints=OracleConstraints(expected_languages=frozenset({"python"})),
    )
    assert codes(findings) == ["C1"]


def test_c1_skipped_when_expected_languages_empty(repo: Path) -> None:
    findings = check_oracle_coherence(
        instruction_text="",
        oracle_files=["docs/README.md"],
        oracle_symbols=[],
        repo_root=repo,
        constraints=OracleConstraints(),
    )
    assert findings == []


def test_d1_path_outside_include(repo: Path) -> None:
    findings = check_oracle_coherence(
        instruction_text="",
        oracle_files=["docs/README.md"],
        oracle_symbols=[],
        repo_root=repo,
        constraints=OracleConstraints(path_include=("src/*",)),
    )
    assert codes(findings) == ["D1"]


def test_d2_path_in_exclude_wins_over_include(repo: Path) -> None:
    findings = check_oracle_coherence(
        instruction_text="",
        oracle_files=["src/foo.py"],
        oracle_symbols=[],
        repo_root=repo,
        constraints=OracleConstraints(
            expected_languages=frozenset({"python"}),
            path_include=("src/*",),
            path_exclude=("*foo*",),
        ),
    )
    assert "D2" in codes(findings)


def test_language_table_override_recognises_custom_extension(repo: Path) -> None:
    custom = {".weird": "python"}
    weird = repo / "src" / "thing.weird"
    weird.write_text("placeholder\n")
    findings = check_oracle_coherence(
        instruction_text="",
        oracle_files=["src/thing.weird"],
        oracle_symbols=[],
        repo_root=repo,
        constraints=OracleConstraints(
            expected_languages=frozenset({"python"}),
            language_extensions=custom,
        ),
    )
    assert findings == []


def test_multiple_findings_emitted_for_same_file(repo: Path) -> None:
    findings = check_oracle_coherence(
        instruction_text="",
        oracle_files=["docs/README.md"],
        oracle_symbols=[],
        repo_root=repo,
        constraints=OracleConstraints(
            expected_languages=frozenset({"python"}),
            path_include=("src/*",),
        ),
    )
    assert "C1" in codes(findings)
    assert "D1" in codes(findings)
