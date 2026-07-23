"""Fail-fast language gate for the Py/Go/JS-TS mining matrix (codeprobe-f7rl.20).

Locked decision 5: mining supports Python, Go, and JavaScript/TypeScript;
everything else must exit with UNSUPPORTED_LANGUAGE before any PR scanning
starts. Comprehension mining is stricter (Python-only). "unknown"
(docs-only/empty repos) must NOT hard-fail — the existing zero-task paths
own that case.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from codeprobe.cli import main
from codeprobe.mining._lang import (
    SUPPORTED_MINING_LANGUAGES,
    detect_repo_language,
)


def _git_repo(root: Path, files: dict[str, str], *, commit: bool = True) -> Path:
    """Create a git repo at *root* containing *files* (staged, optionally committed)."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    if commit:
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=test",
                "commit",
                "-q",
                "-m",
                "init",
            ],
            cwd=root,
            check=True,
        )
    return root


def _combined_output(result) -> str:
    """stdout + stderr regardless of click's CliRunner capture behavior."""
    try:
        stderr = result.stderr
    except ValueError:  # stderr not separately captured
        stderr = ""
    return result.output + (stderr or "")


class TestDetectRepoLanguage:
    def test_detect_repo_language_python(self, tmp_path):
        repo = _git_repo(
            tmp_path / "pyrepo",
            {"a.py": "x = 1\n", "b.py": "y = 2\n", "README.md": "# doc\n"},
        )
        assert detect_repo_language(repo) == "python"

    def test_detect_repo_language_go(self, tmp_path):
        repo = _git_repo(
            tmp_path / "gorepo",
            {"main.go": "package main\n", "util.go": "package main\n"},
        )
        assert detect_repo_language(repo) == "go"

    def test_detect_repo_language_java(self, tmp_path):
        repo = _git_repo(
            tmp_path / "javarepo",
            {"A.java": "class A {}\n", "B.java": "class B {}\n"},
        )
        assert detect_repo_language(repo) == "java"

    def test_detect_repo_language_csharp(self, tmp_path):
        repo = _git_repo(
            tmp_path / "csrepo",
            {"Program.cs": "class Program {}\n", "Util.cs": "class Util {}\n"},
        )
        assert detect_repo_language(repo) == "csharp"

    def test_detect_repo_language_non_git_dir_falls_back_to_walk(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "x.rs").write_text("fn main() {}\n")
        (plain / "y.rs").write_text("fn helper() {}\n")
        assert detect_repo_language(plain) == "rust"

    def test_detect_repo_language_docs_only_is_unknown(self, tmp_path):
        repo = _git_repo(tmp_path / "docsrepo", {"README.md": "# doc\n"})
        assert detect_repo_language(repo) == "unknown"

    def test_supported_matrix_is_exactly_py_go_js_ts(self):
        assert SUPPORTED_MINING_LANGUAGES == frozenset(
            {"python", "go", "javascript", "typescript"}
        )


class TestMineLanguageGate:
    @pytest.mark.parametrize(
        ("language", "files"),
        [
            ("java", {"src/A.java": "class A {}\n", "src/B.java": "class B {}\n"}),
            ("rust", {"src/main.rs": "fn main() {}\n", "src/lib.rs": "pub fn f() {}\n"}),
            (
                "csharp",
                {
                    "src/Program.cs": "class Program {}\n",
                    "src/Util.cs": "class Util {}\n",
                },
            ),
            ("ruby", {"lib/a.rb": "A = 1\n", "lib/b.rb": "B = 2\n"}),
            ("cpp", {"src/main.cpp": "int main() {}\n", "src/util.cpp": "void f() {}\n"}),
        ],
    )
    def test_unsupported_repo_fails_fast(self, language, files, tmp_path):
        """Every criterion-named language exits with UNSUPPORTED_LANGUAGE naming the matrix."""
        repo = _git_repo(tmp_path / f"{language}repo", files)
        result = CliRunner().invoke(
            main, ["mine", str(repo), "--no-interactive"]
        )
        combined = _combined_output(result)
        assert result.exit_code != 0
        assert "UNSUPPORTED_LANGUAGE" in combined
        assert "Python, Go, and JavaScript/TypeScript" in combined
        assert language in combined
        # The misleading zero-task hint must never reach unsupported repos.
        assert "Try a repo with merged PRs" not in combined

    @patch("codeprobe.cli.mine_cmd._dispatch_by_task_type")
    def test_python_repo_passes_gate(self, mock_dispatch, tmp_path):
        """A Python repo sails through the gate and reaches dispatch."""
        repo = _git_repo(
            tmp_path / "pyrepo", {"a.py": "x = 1\n", "b.py": "y = 2\n"}
        )
        result = CliRunner().invoke(
            main, ["mine", str(repo), "--no-interactive"]
        )
        assert mock_dispatch.called, _combined_output(result)
        assert "UNSUPPORTED_LANGUAGE" not in _combined_output(result)

    def test_comprehension_requires_python(self, tmp_path):
        """Go repo + --task-type architecture_comprehension names the alternative."""
        repo = _git_repo(
            tmp_path / "gorepo",
            {"main.go": "package main\n", "util.go": "package main\n"},
        )
        result = CliRunner().invoke(
            main,
            [
                "mine",
                str(repo),
                "--task-type",
                "architecture_comprehension",
                "--no-interactive",
            ],
        )
        combined = _combined_output(result)
        assert result.exit_code != 0
        assert "UNSUPPORTED_LANGUAGE" in combined
        assert "Python-only" in combined
        assert "--goal quality" in combined

    @patch("codeprobe.cli.mine_cmd._dispatch_by_task_type")
    def test_unknown_language_not_blocked(self, mock_dispatch, tmp_path):
        """Docs-only repos are never rejected by the language gate."""
        repo = _git_repo(
            tmp_path / "docsrepo",
            {"README.md": "# doc\n", "docs/guide.md": "# guide\n"},
        )
        result = CliRunner().invoke(
            main, ["mine", str(repo), "--no-interactive"]
        )
        assert "UNSUPPORTED_LANGUAGE" not in _combined_output(result)

    def test_org_scale_secondary_repo_fails_fast(self, tmp_path):
        """An unsupported --repos entry fails the org-scale loop up front."""
        primary = _git_repo(
            tmp_path / "pyrepo", {"a.py": "x = 1\n", "b.py": "y = 2\n"}
        )
        secondary = _git_repo(
            tmp_path / "javarepo",
            {"A.java": "class A {}\n", "B.java": "class B {}\n"},
        )
        result = CliRunner().invoke(
            main,
            [
                "mine",
                str(primary),
                "--org-scale",
                "--repos",
                str(secondary),
                "--no-llm",
                "--no-interactive",
            ],
        )
        combined = _combined_output(result)
        assert result.exit_code != 0
        assert "UNSUPPORTED_LANGUAGE" in combined
        assert "java" in combined
