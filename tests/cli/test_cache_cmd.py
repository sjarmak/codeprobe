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

    def test_purge_refuses_cross_tenant_derived_path(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulate a cross-tenant leak: a mock ``tenant_root`` returns a
        path outside the requested tenant's root. The ``assert_tenant_owned``
        guard at the write boundary must fail closed with
        :class:`CrossTenantAccessError` rather than ``rmtree``-ing the wrong
        directory (INV2).
        """
        from codeprobe.cli import cache_cmd as cache_cmd_mod
        from codeprobe.paths import CrossTenantAccessError

        # Seed BOTH tenants so we have something to (attempt to) delete.
        acme_root = _seed_tenant("acme")
        globex_root = _seed_tenant("globex")

        # Patch tenant_root only inside cache_cmd so the purge command sees
        # globex's path when asked for acme's. The paths-module function
        # keeps normal semantics so assert_tenant_owned evaluates against
        # the correct acme root.
        original_tenant_root = cache_cmd_mod.tenant_root

        def malicious_tenant_root(tenant_id: str) -> Path:
            if tenant_id == "acme":
                # Return globex's directory — a cross-tenant leak.
                return original_tenant_root("globex")
            return original_tenant_root(tenant_id)

        monkeypatch.setattr(cache_cmd_mod, "tenant_root", malicious_tenant_root)

        runner = CliRunner()
        result = runner.invoke(cache, ["purge", "--tenant", "acme"])

        # The guard should have raised CrossTenantAccessError; Click reports
        # it as a non-zero exit with the exception attached.
        assert result.exit_code != 0, result.output
        assert isinstance(result.exception, CrossTenantAccessError)
        # Neither tenant was actually purged.
        assert acme_root.exists(), "acme dir must remain intact"
        assert globex_root.exists(), "globex dir must not be cross-deleted"
