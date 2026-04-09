"""Tests for codeprobe preambles CLI commands."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from codeprobe.cli import main
from codeprobe.cli.preamble_cmd import _extract_vars, _scan_dir


class TestExtractVars:
    def test_no_vars(self) -> None:
        assert _extract_vars("plain text") == []

    def test_single_var(self) -> None:
        assert _extract_vars("Hello {{name}}!") == ["name"]

    def test_multiple_vars(self) -> None:
        assert _extract_vars("{{repo_path}} and {{task_id}}") == [
            "repo_path",
            "task_id",
        ]

    def test_duplicate_vars(self) -> None:
        assert _extract_vars("{{a}} then {{a}} again") == ["a"]

    def test_sorted(self) -> None:
        assert _extract_vars("{{z}} {{a}} {{m}}") == ["a", "m", "z"]


class TestScanDir:
    def test_nonexistent_dir(self, tmp_path: Path) -> None:
        assert _scan_dir(tmp_path / "nope") == []

    def test_empty_dir(self, tmp_path: Path) -> None:
        d = tmp_path / "preambles"
        d.mkdir()
        assert _scan_dir(d) == []

    def test_finds_md_files(self, tmp_path: Path) -> None:
        d = tmp_path / "preambles"
        d.mkdir()
        (d / "alpha.md").write_text("alpha content")
        (d / "beta.md").write_text("beta content")
        (d / "ignore.txt").write_text("not a preamble")
        assert _scan_dir(d) == ["alpha", "beta"]


class TestPreamblesList:
    def test_shows_builtins(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["preambles", "list"])
        assert result.exit_code == 0
        assert "Built-in preambles:" in result.output
        # We know github and sourcegraph exist as built-ins
        assert "github" in result.output
        assert "sourcegraph" in result.output

    def test_shows_template_vars(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["preambles", "list"])
        assert result.exit_code == 0
        # Built-in preambles use template vars like {{repo_path}}
        assert "{{" in result.output

    def test_shows_user_preambles(self, tmp_path: Path, monkeypatch: object) -> None:
        user_dir = tmp_path / ".codeprobe" / "preambles"
        user_dir.mkdir(parents=True)
        (user_dir / "custom.md").write_text("Hello {{name}}")

        import codeprobe.cli.preamble_cmd as mod

        monkeypatch.setattr(mod, "_USER_DIR", user_dir)  # type: ignore[attr-defined]

        runner = CliRunner()
        result = runner.invoke(main, ["preambles", "list"])
        assert result.exit_code == 0
        assert "User preambles" in result.output
        assert "custom" in result.output
        assert "{{name}}" in result.output

    def test_shows_project_preambles(self, tmp_path: Path) -> None:
        project_dir = tmp_path / ".codeprobe" / "preambles"
        project_dir.mkdir(parents=True)
        (project_dir / "local.md").write_text("Project {{task_id}}")

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            # isolated_filesystem sets cwd to tmp_path
            # but we need to create the dir relative to the new cwd
            cwd = Path.cwd()
            proj_dir = cwd / ".codeprobe" / "preambles"
            proj_dir.mkdir(parents=True, exist_ok=True)
            (proj_dir / "local.md").write_text("Project {{task_id}}")

            result = runner.invoke(main, ["preambles", "list"])
            assert result.exit_code == 0
            assert "Project preambles" in result.output
            assert "local" in result.output
            assert "{{task_id}}" in result.output

    def test_no_preambles_found(self, tmp_path: Path, monkeypatch: object) -> None:
        import codeprobe.cli.preamble_cmd as mod

        monkeypatch.setattr(mod, "_USER_DIR", tmp_path / "nope")  # type: ignore[attr-defined]
        # Also need to mock list_builtins to return empty
        monkeypatch.setattr(mod, "list_builtins", lambda: [])  # type: ignore[attr-defined]

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(main, ["preambles", "list"])
            assert result.exit_code == 0
            assert "No preambles found." in result.output
