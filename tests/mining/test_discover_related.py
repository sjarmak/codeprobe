"""Tests for codeprobe.mining.multi_repo.discover_related_repos.

Covers:
- Manifest parsers (go.mod, package.json, pyproject.toml)
- Ranking by AST hit count
- REJECTION of manifest-only candidates with zero AST references
- Empty / missing manifest cases
"""

from __future__ import annotations

import json
from pathlib import Path

from codeprobe.mining.multi_repo import (
    RelatedRepoCandidate,
    _parse_go_mod,
    _parse_package_json,
    _parse_pyproject,
    discover_related_repos,
)

# ---------------------------------------------------------------------------
# Helper: build a tiny repo fixture under tmp_path
# ---------------------------------------------------------------------------


def _write_package_json(root: Path, deps: dict[str, str], dev_deps: dict[str, str] | None = None) -> None:
    payload: dict = {"name": "test-app", "dependencies": deps}
    if dev_deps:
        payload["devDependencies"] = dev_deps
    (root / "package.json").write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# Manifest parser unit tests
# ---------------------------------------------------------------------------


def test_parse_go_mod_block_and_single(tmp_path: Path) -> None:
    gm = tmp_path / "go.mod"
    gm.write_text(
        "module github.com/me/app\n"
        "\n"
        "go 1.22\n"
        "\n"
        "require (\n"
        "    github.com/foo/bar v1.2.3\n"
        "    github.com/baz/qux v0.1.0 // indirect\n"
        ")\n"
        "\n"
        "require github.com/solo/one v2.0.0\n",
        encoding="utf-8",
    )
    names = _parse_go_mod(gm)
    assert "github.com/foo/bar" in names
    assert "github.com/baz/qux" in names
    assert "github.com/solo/one" in names


def test_parse_go_mod_ignores_comments(tmp_path: Path) -> None:
    gm = tmp_path / "go.mod"
    gm.write_text(
        "// comment line\n"
        "require (\n"
        "    // leading comment\n"
        "    github.com/kept/one v1.0.0\n"
        ")\n",
        encoding="utf-8",
    )
    names = _parse_go_mod(gm)
    assert names == {"github.com/kept/one"}


def test_parse_package_json_deps_and_dev_deps(tmp_path: Path) -> None:
    pj = tmp_path / "package.json"
    pj.write_text(
        json.dumps(
            {
                "name": "x",
                "dependencies": {"react": "^18", "@scope/pkg": "1.0.0"},
                "devDependencies": {"vitest": "^1"},
                "peerDependencies": {"react-dom": "^18"},
            }
        ),
        encoding="utf-8",
    )
    names = _parse_package_json(pj)
    assert names == {"react", "@scope/pkg", "vitest", "react-dom"}


def test_parse_package_json_malformed_returns_empty(tmp_path: Path) -> None:
    pj = tmp_path / "package.json"
    pj.write_text("{not valid json", encoding="utf-8")
    assert _parse_package_json(pj) == set()


def test_parse_pyproject_pep621(tmp_path: Path) -> None:
    py = tmp_path / "pyproject.toml"
    py.write_text(
        '[project]\n'
        'name = "x"\n'
        'dependencies = ["click>=8", "pyyaml>=6"]\n'
        '[project.optional-dependencies]\n'
        'dev = ["pytest>=8", "mypy<2"]\n',
        encoding="utf-8",
    )
    names = _parse_pyproject(py)
    assert {"click", "pyyaml", "pytest", "mypy"} <= names


def test_parse_pyproject_poetry(tmp_path: Path) -> None:
    py = tmp_path / "pyproject.toml"
    py.write_text(
        '[tool.poetry]\n'
        'name = "x"\n'
        '[tool.poetry.dependencies]\n'
        'python = "^3.11"\n'
        'requests = "^2.30"\n'
        '[tool.poetry.dev-dependencies]\n'
        'pytest = "^8"\n',
        encoding="utf-8",
    )
    names = _parse_pyproject(py)
    # python itself should NOT be included
    assert "python" not in names
    assert "requests" in names
    assert "pytest" in names


def test_parse_pyproject_malformed_returns_empty(tmp_path: Path) -> None:
    py = tmp_path / "pyproject.toml"
    py.write_text("not = toml = at all = [", encoding="utf-8")
    assert _parse_pyproject(py) == set()


# ---------------------------------------------------------------------------
# discover_related_repos — end-to-end behaviour
# ---------------------------------------------------------------------------


def test_rejects_manifest_only_candidate_with_zero_ast_hits(tmp_path: Path) -> None:
    """Primary declares lodash in package.json but never imports it.

    lodash MUST NOT appear in the ranked output — that's the R8 invariant.
    """
    _write_package_json(tmp_path, {"react": "^18", "lodash": "^4"})
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.js").write_text(
        "import React from 'react';\n"
        "export default function App() { return React.createElement('div'); }\n",
        encoding="utf-8",
    )

    ranked = discover_related_repos(
        tmp_path,
        hints=None,
        interactive=False,
        cross_repo_confirmed=True,
    )

    hints = {c.hint for c in ranked}
    assert "react" in hints
    assert "lodash" not in hints, (
        "lodash declared in package.json but has zero AST references — "
        "must be REJECTED per R8 spec"
    )


def test_returns_candidates_sorted_by_ast_hits(tmp_path: Path) -> None:
    """Candidate with more references ranks ahead of the one with fewer."""
    _write_package_json(tmp_path, {"alpha": "1", "beta": "1"})
    src = tmp_path / "src"
    src.mkdir()
    # 3 references to alpha, 1 reference to beta
    (src / "a.js").write_text(
        "import alpha from 'alpha';\n"
        "import alpha2 from 'alpha';\n"
        "import alpha3 from 'alpha';\n"
        "import beta from 'beta';\n",
        encoding="utf-8",
    )

    ranked = discover_related_repos(
        tmp_path,
        interactive=False,
        cross_repo_confirmed=True,
    )
    hints_in_order = [c.hint for c in ranked]
    assert hints_in_order[0] == "alpha"
    assert "beta" in hints_in_order
    assert hints_in_order.index("alpha") < hints_in_order.index("beta")
    # Verify fields are populated
    alpha = next(c for c in ranked if c.hint == "alpha")
    assert alpha.ast_hits == 3
    assert alpha.manifest_sources == ("package.json",)
    assert isinstance(alpha, RelatedRepoCandidate)


def test_no_manifests_returns_empty(tmp_path: Path) -> None:
    assert (
        discover_related_repos(
            tmp_path, interactive=False, cross_repo_confirmed=True
        )
        == []
    )


def test_multiple_manifests_deduplicate_sources(tmp_path: Path) -> None:
    """If a dep appears in both package.json and pyproject, both sources are listed."""
    _write_package_json(tmp_path, {"shared": "1"})
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["shared"]\n',
        encoding="utf-8",
    )
    (tmp_path / "use.py").write_text("import shared\n", encoding="utf-8")
    (tmp_path / "use.js").write_text("import s from 'shared';\n", encoding="utf-8")

    ranked = discover_related_repos(
        tmp_path, interactive=False, cross_repo_confirmed=True
    )
    assert len(ranked) == 1
    cand = ranked[0]
    assert cand.hint == "shared"
    # sources sorted + deduped
    assert cand.manifest_sources == ("package.json", "pyproject.toml")


def test_go_mod_candidate_ranked(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text(
        "module example.com/app\n"
        "require github.com/foo/bar v1.0.0\n",
        encoding="utf-8",
    )
    (tmp_path / "main.go").write_text(
        'package main\n'
        'import "github.com/foo/bar"\n'
        'func main() { bar.Run() }\n',
        encoding="utf-8",
    )

    ranked = discover_related_repos(
        tmp_path, interactive=False, cross_repo_confirmed=True
    )
    hints = {c.hint for c in ranked}
    assert "github.com/foo/bar" in hints


def test_pyproject_candidate_ranked(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["requests>=2"]\n',
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text(
        "import requests\nrequests.get('https://x')\n",
        encoding="utf-8",
    )

    ranked = discover_related_repos(
        tmp_path, interactive=False, cross_repo_confirmed=True
    )
    assert any(c.hint == "requests" for c in ranked)
