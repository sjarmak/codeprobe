"""Tests for repo hygiene — ensuring .codeprobe/ is excluded from git."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git_init(tmp_path: Path) -> Path:
    """Create a minimal git repo in a temp directory and return its path."""
    subprocess.run(
        ["git", "init", str(tmp_path)],
        capture_output=True,
        check=True,
    )
    return tmp_path


class TestEnsureCodeprobeExcluded:
    """ensure_codeprobe_excluded adds .codeprobe/ to .git/info/exclude."""

    def test_adds_exclude_entry(self, tmp_path: Path) -> None:
        from codeprobe.core.repo_hygiene import ensure_codeprobe_excluded

        repo = _git_init(tmp_path)
        ensure_codeprobe_excluded(repo)

        exclude = repo / ".git" / "info" / "exclude"
        assert exclude.is_file()
        assert ".codeprobe/" in exclude.read_text()

    def test_idempotent(self, tmp_path: Path) -> None:
        from codeprobe.core.repo_hygiene import ensure_codeprobe_excluded

        repo = _git_init(tmp_path)
        ensure_codeprobe_excluded(repo)
        ensure_codeprobe_excluded(repo)

        text = (repo / ".git" / "info" / "exclude").read_text()
        assert text.count(".codeprobe/") == 1

    def test_noop_non_git_dir(self, tmp_path: Path) -> None:
        from codeprobe.core.repo_hygiene import ensure_codeprobe_excluded

        # Should not raise, should not create .git/
        ensure_codeprobe_excluded(tmp_path)
        assert not (tmp_path / ".git").exists()

    def test_creates_info_dir_if_missing(self, tmp_path: Path) -> None:
        from codeprobe.core.repo_hygiene import ensure_codeprobe_excluded

        repo = _git_init(tmp_path)
        # Remove info dir to simulate edge case
        info_dir = repo / ".git" / "info"
        if info_dir.exists():
            import shutil

            shutil.rmtree(info_dir)

        ensure_codeprobe_excluded(repo)

        exclude = repo / ".git" / "info" / "exclude"
        assert exclude.is_file()
        assert ".codeprobe/" in exclude.read_text()

    def test_preserves_existing_exclude_content(self, tmp_path: Path) -> None:
        from codeprobe.core.repo_hygiene import ensure_codeprobe_excluded

        repo = _git_init(tmp_path)
        exclude = repo / ".git" / "info" / "exclude"
        exclude.write_text("# Existing entries\n*.log\n")

        ensure_codeprobe_excluded(repo)

        text = exclude.read_text()
        assert "*.log" in text
        assert ".codeprobe/" in text

    def test_already_excluded_noop(self, tmp_path: Path) -> None:
        from codeprobe.core.repo_hygiene import ensure_codeprobe_excluded

        repo = _git_init(tmp_path)
        exclude = repo / ".git" / "info" / "exclude"
        original = "# Custom rules\n.codeprobe/\n"
        exclude.write_text(original)

        ensure_codeprobe_excluded(repo)
        assert exclude.read_text() == original

    def test_git_status_hides_codeprobe_after_exclude(self, tmp_path: Path) -> None:
        """Integration: after exclusion, git status should not show .codeprobe/."""
        from codeprobe.core.repo_hygiene import ensure_codeprobe_excluded

        repo = _git_init(tmp_path)
        (repo / ".codeprobe" / "tasks").mkdir(parents=True)
        (repo / ".codeprobe" / "tasks" / "foo.txt").write_text("test")

        ensure_codeprobe_excluded(repo)

        result = subprocess.run(
            ["git", "status", "--short"],
            capture_output=True,
            text=True,
            cwd=repo,
        )
        assert ".codeprobe" not in result.stdout
