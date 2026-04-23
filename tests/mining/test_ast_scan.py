"""Tests for codeprobe.mining.ast_scan.

Covers:
- Python scanner (real AST) — imports, from-imports, attribute chains, names
- Go scanner (regex) — import lines and identifier usage
- JS/TS scanner (regex) — ``import`` and ``require``
- Unknown extensions
- Tree traversal
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeprobe.mining.ast_scan import count_references, count_references_in_tree


@pytest.fixture
def tmp_py(tmp_path: Path) -> Path:
    """Create a temp .py file with a fixed body and return the path."""
    p = tmp_path / "app.py"
    p.write_text(
        "import react\n"
        "from react import useState\n"
        "import lodash.debounce\n"
        "import random  # not a target\n"
        "\n"
        "def main():\n"
        "    x = react.Component()\n"
        "    y = lodash.map([], lambda i: i)\n"
        "    return x, y\n",
        encoding="utf-8",
    )
    return p


def test_count_references_python_imports_and_uses(tmp_py: Path) -> None:
    hits = count_references(tmp_py, ["react", "lodash"])
    # react: import + from-import + use (as Name) = 3
    # lodash: import (top-level split) + use (as Name) = 2
    # total should be positive and >= 3 to cover all three access modes.
    assert hits >= 3


def test_count_references_python_unrelated_returns_zero(tmp_py: Path) -> None:
    assert count_references(tmp_py, ["numpy", "django"]) == 0


def test_count_references_python_syntax_error_returns_zero(tmp_path: Path) -> None:
    broken = tmp_path / "broken.py"
    broken.write_text("def oops(:\n    pass\n", encoding="utf-8")
    assert count_references(broken, ["anything"]) == 0


def test_count_references_go_import_and_use(tmp_path: Path) -> None:
    src = tmp_path / "main.go"
    src.write_text(
        'package main\n'
        '\n'
        'import (\n'
        '    "fmt"\n'
        '    "github.com/foo/bar"\n'
        ')\n'
        '\n'
        'func main() {\n'
        '    fmt.Println("hi")\n'
        '    bar.DoThing()\n'
        '}\n',
        encoding="utf-8",
    )
    # Match by full module path
    assert count_references(src, ["github.com/foo/bar"]) >= 1
    # Match by package name (last path component)
    hits = count_references(src, ["bar"])
    assert hits >= 2  # import block + bar.DoThing


def test_count_references_js_import_and_require(tmp_path: Path) -> None:
    src = tmp_path / "index.js"
    src.write_text(
        "import React from 'react';\n"
        "import {useState} from 'react';\n"
        "const lodash = require('lodash');\n"
        "const dyn = import('lodash');\n",
        encoding="utf-8",
    )
    assert count_references(src, ["react"]) == 2
    assert count_references(src, ["lodash"]) == 2


def test_count_references_ts_import(tmp_path: Path) -> None:
    src = tmp_path / "index.ts"
    src.write_text(
        "import type { FC } from 'react';\n"
        "import axios from 'axios';\n",
        encoding="utf-8",
    )
    assert count_references(src, ["react"]) == 1
    assert count_references(src, ["axios"]) == 1


def test_count_references_scoped_js_package(tmp_path: Path) -> None:
    src = tmp_path / "index.js"
    src.write_text(
        "import x from '@scope/pkg';\n"
        "import y from '@scope/pkg/sub';\n",
        encoding="utf-8",
    )
    assert count_references(src, ["@scope/pkg"]) == 2


def test_unknown_extension_returns_zero(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("# react and lodash are great\n", encoding="utf-8")
    assert count_references(readme, ["react", "lodash"]) == 0


def test_empty_targets_returns_zero(tmp_py: Path) -> None:
    assert count_references(tmp_py, []) == 0


def test_missing_file_returns_zero(tmp_path: Path) -> None:
    assert count_references(tmp_path / "does_not_exist.py", ["react"]) == 0


def test_count_references_in_tree_mixed_languages(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("import react\n", encoding="utf-8")
    (tmp_path / "b.js").write_text("import x from 'react';\n", encoding="utf-8")
    (tmp_path / "c.go").write_text(
        'package x\nimport "react"\n', encoding="utf-8"
    )
    (tmp_path / "d.md").write_text("react react react\n", encoding="utf-8")
    # hidden dir must be skipped
    hidden = tmp_path / ".git"
    hidden.mkdir()
    (hidden / "ignored.py").write_text("import react\n", encoding="utf-8")

    hits = count_references_in_tree(tmp_path, ["react"])
    # py:1 + js:1 + go:1 = 3 (md not scanned, .git ignored)
    assert hits == 3


def test_count_references_in_tree_empty_targets(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("import react\n", encoding="utf-8")
    assert count_references_in_tree(tmp_path, []) == 0


def test_count_references_in_tree_missing_root(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    assert count_references_in_tree(missing, ["react"]) == 0
