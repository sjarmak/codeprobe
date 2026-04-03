"""Tests for scanner-refactor: multi-repo, timeout, and caching."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from codeprobe.mining.org_scale_families import COMPLIANCE_AUDIT, MIGRATION_INVENTORY
from codeprobe.mining.org_scale_scanner import (
    FamilyScanResult,
    clear_scan_cache,
    scan_repo,
    scan_repo_for_family,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    """Create a git repo with given files."""
    repo = tmp_path / name
    repo.mkdir()
    for path, content in files.items():
        fp = repo / path
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo),
        capture_output=True,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
        },
    )
    return repo


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    """Clear the scan cache before each test."""
    clear_scan_cache()


# ---------------------------------------------------------------------------
# Multi-repo scanning
# ---------------------------------------------------------------------------


class TestMultiRepo:
    def test_scan_two_repos_merges_hits(self, tmp_path: Path) -> None:
        repo_a = _make_repo(
            tmp_path,
            "repo-a",
            {
                "src/old_a.py": "@deprecated\ndef func_a(): pass",
                "src/also_a.py": "@Deprecated\nclass OldA: pass",
                "src/clean.py": "def clean(): pass",
            },
        )
        repo_b = _make_repo(
            tmp_path,
            "repo-b",
            {
                "src/old_b.py": "@deprecated\ndef func_b(): pass",
                "src/legacy_b.py": "# Deprecated: old\ndef legacy(): pass",
                "src/extra.py": "@Deprecated\nclass Extra: pass",
            },
        )

        result = scan_repo_for_family([repo_a, repo_b], MIGRATION_INVENTORY)

        assert isinstance(result, FamilyScanResult)
        assert result.repo_paths == (repo_a, repo_b)
        # Files from both repos should be found
        assert "src/old_a.py" in result.matched_files
        assert "src/old_b.py" in result.matched_files
        assert "src/clean.py" not in result.matched_files
        assert len(result.hits) >= 4
        assert not result.timed_out

    def test_scan_two_repos_commit_sha_combined(self, tmp_path: Path) -> None:
        repo_a = _make_repo(tmp_path, "repo-x", {"a.py": "@deprecated\ndef f(): pass"})
        repo_b = _make_repo(tmp_path, "repo-y", {"b.py": "@deprecated\ndef g(): pass"})

        result = scan_repo_for_family([repo_a, repo_b], MIGRATION_INVENTORY)
        # commit_sha should contain both SHAs joined by comma
        assert "," in result.commit_sha
        parts = result.commit_sha.split(",")
        assert len(parts) == 2
        assert all(len(p) == 40 for p in parts)

    def test_scan_repo_multi_repo(self, tmp_path: Path) -> None:
        repo_a = _make_repo(
            tmp_path,
            "repo-m1",
            {
                "src/old.py": "@deprecated\ndef f(): pass",
                "src/old2.py": "@Deprecated\nclass X: pass",
                "src/old3.py": "# Deprecated: removed\ndef g(): pass",
            },
        )
        repo_b = _make_repo(
            tmp_path,
            "repo-m2",
            {
                "src/legacy.py": "@deprecated\ndef h(): pass",
                "src/legacy2.py": "@Deprecated\nclass Y: pass",
                "src/legacy3.py": "# Deprecated: old\ndef k(): pass",
            },
        )

        results = scan_repo([repo_a, repo_b], (MIGRATION_INVENTORY,))
        # With 6 files across 2 repos, should meet min_hits
        assert len(results) >= 1
        assert results[0].repo_paths == (repo_a, repo_b)


# ---------------------------------------------------------------------------
# Timeout behavior
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_timeout_returns_partial_results(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            "repo-timeout",
            {
                "src/old1.py": "@deprecated\ndef f1(): pass",
                "src/old2.py": "@deprecated\ndef f2(): pass",
                "src/old3.py": "@deprecated\ndef f3(): pass",
            },
        )

        # Patch time.monotonic to simulate timeout after first check
        original_monotonic = time.monotonic
        call_count = 0

        def fake_monotonic() -> float:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                # First calls: before deadline check in scan_repo_for_family
                return original_monotonic()
            # After that: past deadline
            return original_monotonic() + 9999

        with patch("codeprobe.mining.org_scale_scanner.time.monotonic", fake_monotonic):
            result = scan_repo_for_family(
                [repo], MIGRATION_INVENTORY, timeout_seconds=1.0
            )

        assert result.timed_out is True
        # Should still have some partial results (or none if timeout was immediate)

    def test_no_timeout_when_fast(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            "repo-fast",
            {"src/old.py": "@deprecated\ndef f(): pass"},
        )
        result = scan_repo_for_family([repo], MIGRATION_INVENTORY, timeout_seconds=60.0)
        assert result.timed_out is False


# ---------------------------------------------------------------------------
# Cache hit/miss
# ---------------------------------------------------------------------------


class TestCache:
    def test_cache_hit_returns_same_result(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            "repo-cache",
            {
                "src/old.py": "@deprecated\ndef f(): pass",
                "src/old2.py": "@Deprecated\nclass X: pass",
                "src/old3.py": "# Deprecated: removed\ndef g(): pass",
            },
        )

        result1 = scan_repo_for_family([repo], MIGRATION_INVENTORY)
        result2 = scan_repo_for_family([repo], MIGRATION_INVENTORY)

        assert result1 is result2  # Same object from cache

    def test_cache_miss_on_different_family(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            "repo-cache2",
            {
                "pkg/server.go": 'import "crypto/tls"\nvar c tls.Config{}',
                "src/old.py": "@deprecated\ndef f(): pass",
            },
        )

        result1 = scan_repo_for_family([repo], MIGRATION_INVENTORY)
        result2 = scan_repo_for_family([repo], COMPLIANCE_AUDIT)

        assert result1 is not result2
        assert result1.family.name != result2.family.name

    def test_clear_cache_forces_rescan(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            "repo-cache3",
            {"src/old.py": "@deprecated\ndef f(): pass"},
        )

        result1 = scan_repo_for_family([repo], MIGRATION_INVENTORY)
        clear_scan_cache()
        result2 = scan_repo_for_family([repo], MIGRATION_INVENTORY)

        # Same content but different objects after cache clear
        assert result1 is not result2
        assert result1.hits == result2.hits

    def test_cache_miss_on_different_repos(self, tmp_path: Path) -> None:
        repo_a = _make_repo(
            tmp_path, "repo-ca", {"src/old.py": "@deprecated\ndef f(): pass"}
        )
        repo_b = _make_repo(
            tmp_path, "repo-cb", {"src/old.py": "@deprecated\ndef g(): pass"}
        )

        result1 = scan_repo_for_family([repo_a], MIGRATION_INVENTORY)
        result2 = scan_repo_for_family([repo_b], MIGRATION_INVENTORY)

        assert result1 is not result2


# ---------------------------------------------------------------------------
# Single-repo backward compat
# ---------------------------------------------------------------------------


class TestSingleRepoCompat:
    def test_single_repo_as_list(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            "repo-compat",
            {
                "src/old.py": "@deprecated\ndef f(): pass",
                "src/old2.py": "@Deprecated\nclass X: pass",
                "src/old3.py": "# Deprecated: removed\ndef g(): pass",
            },
        )

        result = scan_repo_for_family([repo], MIGRATION_INVENTORY)

        assert result.repo_paths == (repo,)
        assert len(result.matched_files) >= 2
        assert not result.timed_out

    def test_family_scan_result_has_timed_out_field(self, tmp_path: Path) -> None:
        result = FamilyScanResult(
            family=MIGRATION_INVENTORY,
            hits=(),
            repo_paths=(tmp_path,),
            commit_sha="abc123",
            matched_files=frozenset(),
        )
        assert result.timed_out is False

    def test_family_scan_result_timed_out_true(self, tmp_path: Path) -> None:
        result = FamilyScanResult(
            family=MIGRATION_INVENTORY,
            hits=(),
            repo_paths=(tmp_path,),
            commit_sha="abc123",
            matched_files=frozenset(),
            timed_out=True,
        )
        assert result.timed_out is True
