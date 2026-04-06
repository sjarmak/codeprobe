"""Tests for discover_changed_symbols() — change-scope-audit scanner."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from codeprobe.mining.org_scale_scanner import (
    clear_scan_cache,
    discover_changed_symbols,
    get_tracked_files,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@test.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@test.com",
    "PATH": "/usr/bin:/bin",
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**_GIT_ENV, "HOME": str(repo.parent)}
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )


def _make_repo_with_change(tmp_path: Path) -> Path:
    """Create a git repo with two commits: initial + signature change.

    Commit 1: defines calculate_total() used in 3 files
    Commit 2: modifies calculate_total() signature
    """
    repo = tmp_path / "change-repo"
    repo.mkdir()

    # Initial files
    (repo / "lib").mkdir()
    (repo / "lib" / "math_utils.py").write_text(
        "def calculate_total(items):\n    return sum(items)\n"
    )
    (repo / "app.py").write_text(
        "from lib.math_utils import calculate_total\n"
        "result = calculate_total([1, 2, 3])\n"
    )
    (repo / "cli.py").write_text(
        "from lib.math_utils import calculate_total\n"
        "print(calculate_total([10, 20]))\n"
    )
    (repo / "service.py").write_text(
        "from lib.math_utils import calculate_total\n"
        "def run():\n"
        "    return calculate_total([5, 6])\n"
    )
    (repo / "tests.py").write_text(
        "from lib.math_utils import calculate_total\n"
        "assert calculate_total([1]) == 1\n"
    )
    (repo / "worker.py").write_text(
        "from lib.math_utils import calculate_total\n"
        "def process():\n"
        "    return calculate_total([7, 8, 9])\n"
    )
    (repo / "handler.py").write_text(
        "from lib.math_utils import calculate_total\n"
        "total = calculate_total([100])\n"
    )

    _git(repo, "init")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")

    # Second commit: modify calculate_total signature
    (repo / "lib" / "math_utils.py").write_text(
        "def calculate_total(items, *, tax_rate=0.0):\n"
        "    return sum(items) * (1 + tax_rate)\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add tax_rate param to calculate_total")

    return repo


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    clear_scan_cache()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDiscoverChangedSymbols:
    def test_finds_changed_symbol_and_references(self, tmp_path: Path) -> None:
        repo = _make_repo_with_change(tmp_path)
        tracked = get_tracked_files(repo)

        results = discover_changed_symbols([repo], tracked, "python")

        assert len(results) >= 1
        commit_sha, symbol, def_file, ref_files = results[0]

        # Should find the calculate_total symbol
        assert symbol == "calculate_total"
        assert "lib/math_utils.py" in def_file
        assert len(commit_sha) == 40

        # Should find referencing files (app.py, cli.py, service.py, tests.py)
        assert len(ref_files) >= 3
        assert "app.py" in ref_files or "cli.py" in ref_files

    def test_empty_repo_returns_empty(self, tmp_path: Path) -> None:
        repo = tmp_path / "empty-repo"
        repo.mkdir()
        _git(repo, "init")
        (repo / "dummy.py").write_text("# empty\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "init")

        tracked = get_tracked_files(repo)
        results = discover_changed_symbols([repo], tracked, "python")
        assert results == []

    def test_filters_short_symbol_names(self, tmp_path: Path) -> None:
        """Symbols shorter than 8 chars should be filtered out."""
        repo = tmp_path / "short-names"
        repo.mkdir()

        (repo / "lib.py").write_text("def run(x):\n    return x\n")
        (repo / "a.py").write_text("from lib import run\nrun(1)\n")
        (repo / "b.py").write_text("from lib import run\nrun(2)\n")
        (repo / "c.py").write_text("from lib import run\nrun(3)\n")
        (repo / "d.py").write_text("from lib import run\nrun(4)\n")
        (repo / "e.py").write_text("from lib import run\nrun(5)\n")
        (repo / "f.py").write_text("from lib import run\nrun(6)\n")

        _git(repo, "init")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "init")

        (repo / "lib.py").write_text("def run(x, y=0):\n    return x + y\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "change run")

        tracked = get_tracked_files(repo)
        results = discover_changed_symbols([repo], tracked, "python")

        # "run" is only 3 chars — should be filtered
        symbol_names = [r[1] for r in results]
        assert "run" not in symbol_names

    def test_respects_recent_n_limit(self, tmp_path: Path) -> None:
        repo = _make_repo_with_change(tmp_path)
        tracked = get_tracked_files(repo)

        # With recent_n=0, should find nothing
        results = discover_changed_symbols([repo], tracked, "python", recent_n=0)
        assert results == []

    def test_returns_at_most_3_results(self, tmp_path: Path) -> None:
        """Results capped at 3."""
        repo = tmp_path / "many-changes"
        repo.mkdir()

        # Create initial files with many long-named functions
        funcs = [
            "process_payment",
            "validate_address",
            "calculate_shipping",
            "generate_invoice",
            "transform_record",
        ]
        lib_content = "\n".join(f"def {fn}(x):\n    return x\n" for fn in funcs)
        (repo / "lib.py").write_text(lib_content)

        # Create 6 referencing files for each function
        for i in range(6):
            lines = [f"from lib import {fn}" for fn in funcs]
            lines += [f"{fn}({i})" for fn in funcs]
            (repo / f"user_{i}.py").write_text("\n".join(lines) + "\n")

        _git(repo, "init")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "init")

        # Modify all functions in separate commits
        for fn in funcs:
            lib_content = (repo / "lib.py").read_text()
            lib_content = lib_content.replace(
                f"def {fn}(x):", f"def {fn}(x, flag=False):"
            )
            (repo / "lib.py").write_text(lib_content)
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", f"change {fn}")

        tracked = get_tracked_files(repo)
        results = discover_changed_symbols([repo], tracked, "python")

        # Should return at most 3
        assert len(results) <= 3

    def test_blocklisted_symbols_excluded(self, tmp_path: Path) -> None:
        """Symbols in the blocklist should not appear."""
        repo = tmp_path / "blocklist-test"
        repo.mkdir()

        # "create" and "delete" and "validate" are in _SYMBOL_BLOCKLIST
        # but "validate" is only 8 chars... use something in blocklist that's 8+ chars
        # _SYMBOL_BLOCKLIST has "validate" which is 8 chars
        (repo / "lib.py").write_text("def validate(x):\n    return x\n")
        for i in range(6):
            (repo / f"f{i}.py").write_text("from lib import validate\nvalidate(1)\n")

        _git(repo, "init")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "init")

        (repo / "lib.py").write_text("def validate(x, strict=True):\n    return x\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "change validate")

        tracked = get_tracked_files(repo)
        results = discover_changed_symbols([repo], tracked, "python")

        symbol_names = [r[1] for r in results]
        assert "validate" not in symbol_names
