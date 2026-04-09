"""Tests for the codeprobe auth CLI command group."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from codeprobe.cli import main
from codeprobe.mining.sg_auth import CachedToken


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME at a temp directory so cache writes are isolated."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SRC_ACCESS_TOKEN", raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# codeprobe auth sourcegraph
# ---------------------------------------------------------------------------


class TestAuthSourcegraph:
    """Test the `codeprobe auth sourcegraph` command."""

    def test_prompts_and_caches_pat(self, runner: CliRunner, tmp_home: Path) -> None:
        result = runner.invoke(
            main, ["auth", "sourcegraph"], input="sgp_test_token_123\n"
        )
        assert result.exit_code == 0
        assert "Authenticated" in result.output
        assert "auth.json" in result.output

        # Verify token was cached
        from codeprobe.mining.sg_auth import load_cached_token

        cached = load_cached_token()
        assert cached is not None
        assert cached.access_token == "sgp_test_token_123"

    def test_custom_endpoint(self, runner: CliRunner, tmp_home: Path) -> None:
        result = runner.invoke(
            main,
            ["auth", "sourcegraph", "--endpoint", "https://sg.corp.com"],
            input="sgp_corp_token\n",
        )
        assert result.exit_code == 0
        assert "sg.corp.com" in result.output

        from codeprobe.mining.sg_auth import load_cached_token

        cached = load_cached_token("https://sg.corp.com")
        assert cached is not None
        assert cached.endpoint == "https://sg.corp.com"


# ---------------------------------------------------------------------------
# codeprobe auth logout
# ---------------------------------------------------------------------------


class TestAuthLogout:
    """Test the `codeprobe auth logout` command."""

    def test_clears_cached_token(self, runner: CliRunner, tmp_home: Path) -> None:
        # First, cache a token
        from codeprobe.mining.sg_auth import save_cached_token

        save_cached_token(
            CachedToken(
                access_token="tok",
                refresh_token=None,
                expires_at=None,
                endpoint="https://sourcegraph.com",
            )
        )

        result = runner.invoke(main, ["auth", "logout"])
        assert result.exit_code == 0
        assert "Cleared" in result.output

        from codeprobe.mining.sg_auth import load_cached_token

        assert load_cached_token() is None

    def test_logout_no_cache_is_ok(self, runner: CliRunner, tmp_home: Path) -> None:
        """Logout when no cache exists should succeed gracefully."""
        result = runner.invoke(main, ["auth", "logout"])
        assert result.exit_code == 0
        assert "Cleared" in result.output


# ---------------------------------------------------------------------------
# codeprobe auth status
# ---------------------------------------------------------------------------


class TestAuthStatus:
    """Test the `codeprobe auth status` command."""

    def test_status_empty(self, runner: CliRunner, tmp_home: Path) -> None:
        result = runner.invoke(main, ["auth", "status"])
        assert result.exit_code == 0
        assert "Not authenticated" in result.output

    def test_status_cached(self, runner: CliRunner, tmp_home: Path) -> None:
        from codeprobe.mining.sg_auth import save_cached_token

        expires = datetime.now(UTC) + timedelta(hours=24)
        save_cached_token(
            CachedToken(
                access_token="tok",
                refresh_token=None,
                expires_at=expires,
                endpoint="https://sourcegraph.com",
            )
        )

        result = runner.invoke(main, ["auth", "status"])
        assert result.exit_code == 0
        assert "sourcegraph.com" in result.output
        assert "Expires" in result.output

    def test_status_no_expiry(self, runner: CliRunner, tmp_home: Path) -> None:
        from codeprobe.mining.sg_auth import save_cached_token

        save_cached_token(
            CachedToken(
                access_token="tok",
                refresh_token=None,
                expires_at=None,
                endpoint="https://sourcegraph.com",
            )
        )

        result = runner.invoke(main, ["auth", "status"])
        assert result.exit_code == 0
        assert "never" in result.output.lower() or "unknown" in result.output.lower()
