"""Tests for reference_filter — import-dependency filtering for org-scale GT.

The org-scale mining pipeline (symbol-reference-trace and change-scope-audit
families) builds ground truth by token-matching the symbol name. For symbols
whose name collides with stdlib (e.g. ``MkdirAll``, ``ReadFile``, ``WriteFile``
in Go; ``open``, ``read`` in Python), the resulting file list includes files
that only call the stdlib version and have no dependency on the defining
package.

This filter removes such files by keeping only candidates that either
(a) live in the same package directory as the defining file, or
(b) import the defining package.
"""

from __future__ import annotations

from pathlib import Path

from codeprobe.mining.reference_filter import filter_by_import_dependency

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------


def test_go_filters_out_stdlib_only_callers(tmp_path: Path) -> None:
    """Files that only call os.MkdirAll get filtered; callers of the defining
    package are kept."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "go.mod", "module github.com/example/proj\n")

    # Defining package
    _write(
        repo,
        "internal/fsys/fake.go",
        "package fsys\n\ntype Fake struct{}\n"
        "func (f *Fake) MkdirAll(p string, _ int) error { return nil }\n",
    )
    # Same package test file — no import needed
    _write(
        repo,
        "internal/fsys/fake_test.go",
        "package fsys\n\nfunc TestMkdir() { f := &Fake{}; f.MkdirAll(\"/\", 0) }\n",
    )
    # Caller that imports the defining package
    _write(
        repo,
        "cmd/init.go",
        'package main\n\nimport "github.com/example/proj/internal/fsys"\n\n'
        "func Init() { f := &fsys.Fake{}; f.MkdirAll(\"/tmp\", 0) }\n",
    )
    # Caller that only uses stdlib os.MkdirAll — MUST be filtered
    _write(
        repo,
        "cmd/util.go",
        'package main\n\nimport "os"\n\n'
        'func Util() { os.MkdirAll("/tmp", 0) }\n',
    )
    # Another stdlib-only caller (with many occurrences of the token)
    _write(
        repo,
        "test/helpers/binary_test.go",
        'package helpers\n\nimport "os"\n\n'
        'func Setup() { os.MkdirAll("/a", 0); os.MkdirAll("/b", 0) }\n',
    )

    candidates = frozenset(
        {
            "internal/fsys/fake.go",
            "internal/fsys/fake_test.go",
            "cmd/init.go",
            "cmd/util.go",
            "test/helpers/binary_test.go",
        }
    )

    kept = filter_by_import_dependency(
        candidate_files=candidates,
        defining_file="internal/fsys/fake.go",
        repo_path=repo,
        language="go",
    )

    assert kept == {
        "internal/fsys/fake.go",  # defining file
        "internal/fsys/fake_test.go",  # same package
        "cmd/init.go",  # imports defining package
    }


def test_go_keeps_aliased_imports(tmp_path: Path) -> None:
    """Aliased imports (e.g. ``fs "github.com/.../fsys"``) still contain the
    literal package path and must be kept."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "go.mod", "module github.com/example/proj\n")
    _write(repo, "internal/fsys/fake.go", "package fsys\n")
    _write(
        repo,
        "cmd/aliased.go",
        'package main\n\nimport fs "github.com/example/proj/internal/fsys"\n\n'
        "func X() { _ = fs.Fake{} }\n",
    )

    kept = filter_by_import_dependency(
        candidate_files=frozenset({"cmd/aliased.go"}),
        defining_file="internal/fsys/fake.go",
        repo_path=repo,
        language="go",
    )
    assert kept == {"cmd/aliased.go"}


def test_go_no_gomod_preserves_input(tmp_path: Path) -> None:
    """No go.mod → no reliable module path → cannot filter, return input
    unchanged (fail-safe: prefer false positives over false negatives)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo, "a.go", "package main\n")

    candidates = frozenset({"a.go", "b.go"})
    kept = filter_by_import_dependency(
        candidate_files=candidates,
        defining_file="a.go",
        repo_path=repo,
        language="go",
    )
    assert kept == candidates


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------


def test_python_filters_out_stdlib_only_callers(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(
        repo,
        "src/core/base_handler.py",
        "def process_configuration(c): return c\n",
    )
    _write(repo, "src/core/__init__.py", "")
    # Importer — keep
    _write(
        repo,
        "src/modules/mod_a.py",
        "from src.core.base_handler import process_configuration\n\n"
        "def go(): return process_configuration({})\n",
    )
    # Import via package — keep
    _write(
        repo,
        "src/modules/mod_b.py",
        "from src.core import base_handler\n\n"
        "def go(): return base_handler.process_configuration({})\n",
    )
    # Unrelated stdlib-only — filter (module has the token but doesn't import
    # the defining package)
    _write(
        repo,
        "src/util/shadow.py",
        "# Local helper\n"
        "def process_configuration(c): return c\n"
        "def go(): return process_configuration({})\n",
    )

    candidates = frozenset(
        {
            "src/core/base_handler.py",
            "src/modules/mod_a.py",
            "src/modules/mod_b.py",
            "src/util/shadow.py",
        }
    )
    kept = filter_by_import_dependency(
        candidate_files=candidates,
        defining_file="src/core/base_handler.py",
        repo_path=repo,
        language="python",
    )
    assert kept == {
        "src/core/base_handler.py",
        "src/modules/mod_a.py",
        "src/modules/mod_b.py",
    }


# ---------------------------------------------------------------------------
# Unknown language → no-op
# ---------------------------------------------------------------------------


def test_unknown_language_returns_input_unchanged(tmp_path: Path) -> None:
    candidates = frozenset({"a.rs", "b.rs"})
    kept = filter_by_import_dependency(
        candidate_files=candidates,
        defining_file="src/lib.rs",
        repo_path=tmp_path,
        language="rust",
    )
    assert kept == candidates
