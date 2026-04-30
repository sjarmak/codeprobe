"""Tests for the symbol-resolution helper.

We pin the regex fallback path here to keep the suite independent of whether
ast-grep is installed in the test environment. The ast-grep branch is exercised
in the integration tests downstream.
"""

from __future__ import annotations

from pathlib import Path

from codeprobe.qa.benchmark_qa_core._symbols import symbol_in_file


def test_symbol_found_via_regex_fallback(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("def alpha():\n    return 1\n")
    assert symbol_in_file(f, "alpha", prefer_astgrep=False) is True


def test_symbol_missing_via_regex_fallback(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("def alpha():\n    return 1\n")
    assert symbol_in_file(f, "beta", prefer_astgrep=False) is False


def test_word_boundary_avoids_substring_match(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("alphabet = 1\n")
    assert symbol_in_file(f, "alpha", prefer_astgrep=False) is False


def test_unreadable_file_returns_false(tmp_path: Path) -> None:
    missing = tmp_path / "nope.py"
    assert symbol_in_file(missing, "anything", prefer_astgrep=False) is False


def test_binary_file_does_not_crash(tmp_path: Path) -> None:
    f = tmp_path / "blob.bin"
    f.write_bytes(b"\x00\x01\x02not_a_match\xff\xfe")
    assert symbol_in_file(f, "missing_symbol", prefer_astgrep=False) is False
