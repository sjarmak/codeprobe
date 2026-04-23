"""Tests for ``codeprobe cache purge`` — tenant-scoped cache purge (INV2)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli.cache_cmd import cache
from codeprobe.paths import tenant_root


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate ~/.codeprobe under a per-test temp HOME."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _seed_tenant(tenant_id: str, *, with_content: bool = True) -> Path:
    """Create a tenant state dir with an optional marker file, return root."""
    root = tenant_root(tenant_id)
    root.mkdir(parents=True, exist_ok=True)
    if with_content:
        (root / "repo-deadbeef0000dead").mkdir(parents=True, exist_ok=True)
        (root / "repo-deadbeef0000dead" / "marker.txt").write_text("keep-me")
    return root


class TestCachePurge:
    def test_purge_removes_tenant_dir(
        self, runner: CliRunner, tmp_home: Path
    ) -> None:
        root = _seed_tenant("acme")
        assert root.exists()

        result = runner.invoke(cache, ["purge", "--tenant", "acme"])

        assert result.exit_code == 0, result.output
        assert not root.exists()
        assert "acme" in result.output

    def test_purge_is_tenant_scoped(
        self, runner: CliRunner, tmp_home: Path
    ) -> None:
        acme_root = _seed_tenant("acme")
        globex_root = _seed_tenant("globex")

        result = runner.invoke(cache, ["purge", "--tenant", "acme"])

        assert result.exit_code == 0, result.output
        assert not acme_root.exists()
        assert globex_root.exists(), "sibling tenant must survive purge"
        assert (globex_root / "repo-deadbeef0000dead" / "marker.txt").read_text() == (
            "keep-me"
        )

    def test_purge_missing_dir_exits_zero(
        self, runner: CliRunner, tmp_home: Path
    ) -> None:
        result = runner.invoke(cache, ["purge", "--tenant", "acme"])

        assert result.exit_code == 0, result.output
        assert "nothing to purge" in result.output.lower()

    def test_missing_tenant_flag_exits_nonzero(
        self, runner: CliRunner, tmp_home: Path
    ) -> None:
        result = runner.invoke(cache, ["purge"])

        assert result.exit_code != 0
        combined = (result.output or "") + (
            result.stderr if result.stderr_bytes is not None else ""
        )
        assert "--tenant" in combined or "tenant" in combined.lower()
