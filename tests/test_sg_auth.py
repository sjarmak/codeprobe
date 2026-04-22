"""Tests for Sourcegraph auth token cache and retrieval."""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from codeprobe.mining import sg_auth
from codeprobe.mining.sg_auth import CachedToken


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME at a temp directory so cache writes are isolated."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Ensure env var fallback doesn't leak between tests — clear all
    # accepted Sourcegraph token env vars, not just the canonical one.
    for name in sg_auth._ACCEPTED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _future(seconds: int = 3600) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def _past(seconds: int = 60) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# CachedToken
# ---------------------------------------------------------------------------


class TestCachedToken:
    def test_is_expired_false_when_future(self) -> None:
        token = CachedToken(
            access_token="t",
            refresh_token=None,
            expires_at=_future(3600),
            endpoint="https://sourcegraph.com",
        )
        assert token.is_expired() is False

    def test_is_expired_true_when_past(self) -> None:
        token = CachedToken(
            access_token="t",
            refresh_token=None,
            expires_at=_past(60),
            endpoint="https://sourcegraph.com",
        )
        assert token.is_expired() is True

    def test_is_expired_false_when_expires_at_none(self) -> None:
        """PATs have no expiry; treat as non-expiring."""
        token = CachedToken(
            access_token="t",
            refresh_token=None,
            expires_at=None,
            endpoint="https://sourcegraph.com",
        )
        assert token.is_expired() is False

    def test_is_frozen(self) -> None:
        token = CachedToken(
            access_token="t",
            refresh_token=None,
            expires_at=None,
            endpoint="https://sourcegraph.com",
        )
        with pytest.raises(Exception):
            token.access_token = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# save_cached_token / load_cached_token
# ---------------------------------------------------------------------------


class TestCachePersistence:
    def test_save_creates_file_with_0600_permissions(self, tmp_home: Path) -> None:
        token = CachedToken(
            access_token="secret",
            refresh_token=None,
            expires_at=None,
            endpoint="https://sourcegraph.com",
        )
        sg_auth.save_cached_token(token)

        cache_path = tmp_home / ".codeprobe" / "auth.json"
        assert cache_path.exists()
        mode = stat.S_IMODE(cache_path.stat().st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    def test_save_creates_parent_dir_with_0700_permissions(
        self, tmp_home: Path
    ) -> None:
        token = CachedToken(
            access_token="secret",
            refresh_token=None,
            expires_at=None,
            endpoint="https://sourcegraph.com",
        )
        sg_auth.save_cached_token(token)

        parent = tmp_home / ".codeprobe"
        assert parent.is_dir()
        mode = stat.S_IMODE(parent.stat().st_mode)
        assert mode == 0o700, f"expected 0700, got {oct(mode)}"

    def test_load_returns_none_when_missing(self, tmp_home: Path) -> None:
        assert sg_auth.load_cached_token() is None

    def test_load_returns_none_for_unknown_endpoint(self, tmp_home: Path) -> None:
        token = CachedToken(
            access_token="secret",
            refresh_token=None,
            expires_at=None,
            endpoint="https://sourcegraph.com",
        )
        sg_auth.save_cached_token(token)
        assert sg_auth.load_cached_token("https://other.example.com") is None

    def test_round_trip_without_expiry(self, tmp_home: Path) -> None:
        original = CachedToken(
            access_token="secret",
            refresh_token=None,
            expires_at=None,
            endpoint="https://sourcegraph.com",
        )
        sg_auth.save_cached_token(original)
        loaded = sg_auth.load_cached_token()
        assert loaded == original

    def test_round_trip_with_expiry_and_refresh(self, tmp_home: Path) -> None:
        expiry = datetime(2030, 1, 1, tzinfo=UTC)
        original = CachedToken(
            access_token="secret",
            refresh_token="refresh",
            expires_at=expiry,
            endpoint="https://sourcegraph.com",
        )
        sg_auth.save_cached_token(original)
        loaded = sg_auth.load_cached_token()
        assert loaded == original

    def test_save_preserves_other_endpoints(self, tmp_home: Path) -> None:
        a = CachedToken(
            access_token="a",
            refresh_token=None,
            expires_at=None,
            endpoint="https://sourcegraph.com",
        )
        b = CachedToken(
            access_token="b",
            refresh_token=None,
            expires_at=None,
            endpoint="https://enterprise.example.com",
        )
        sg_auth.save_cached_token(a)
        sg_auth.save_cached_token(b)

        assert sg_auth.load_cached_token("https://sourcegraph.com") == a
        assert sg_auth.load_cached_token("https://enterprise.example.com") == b

    def test_save_rewrites_permissions_on_overwrite(self, tmp_home: Path) -> None:
        token = CachedToken(
            access_token="a",
            refresh_token=None,
            expires_at=None,
            endpoint="https://sourcegraph.com",
        )
        sg_auth.save_cached_token(token)
        cache_path = tmp_home / ".codeprobe" / "auth.json"
        os.chmod(cache_path, 0o644)
        sg_auth.save_cached_token(token)
        assert stat.S_IMODE(cache_path.stat().st_mode) == 0o600

    def test_load_corrupt_file_returns_none(self, tmp_home: Path) -> None:
        cache_dir = tmp_home / ".codeprobe"
        cache_dir.mkdir(mode=0o700)
        (cache_dir / "auth.json").write_text("not json{{")
        assert sg_auth.load_cached_token() is None


# ---------------------------------------------------------------------------
# get_valid_token
# ---------------------------------------------------------------------------


class TestGetValidToken:
    def test_env_var_takes_precedence_over_cache(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cached = CachedToken(
            access_token="from-cache",
            refresh_token=None,
            expires_at=None,
            endpoint="https://sourcegraph.com",
        )
        sg_auth.save_cached_token(cached)
        monkeypatch.setenv("SRC_ACCESS_TOKEN", "from-env")

        token = sg_auth.get_valid_token()
        assert token.access_token == "from-env"
        assert token.endpoint == "https://sourcegraph.com"

    def test_env_var_does_not_touch_cache(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SRC_ACCESS_TOKEN", "from-env")
        sg_auth.get_valid_token()
        assert not (tmp_home / ".codeprobe" / "auth.json").exists()

    def test_returns_cached_token_when_valid(self, tmp_home: Path) -> None:
        cached = CachedToken(
            access_token="cached",
            refresh_token=None,
            expires_at=_future(3600),
            endpoint="https://sourcegraph.com",
        )
        sg_auth.save_cached_token(cached)
        token = sg_auth.get_valid_token()
        assert token.access_token == "cached"

    def test_refreshes_expired_cached_token(self, tmp_home: Path) -> None:
        expired = CachedToken(
            access_token="old",
            refresh_token="refresh-me",
            expires_at=_past(60),
            endpoint="https://sourcegraph.com",
        )
        sg_auth.save_cached_token(expired)

        refreshed = CachedToken(
            access_token="new",
            refresh_token="refresh-me",
            expires_at=_future(3600),
            endpoint="https://sourcegraph.com",
        )
        with patch.object(sg_auth, "refresh_token", return_value=refreshed) as mock:
            token = sg_auth.get_valid_token()
            mock.assert_called_once()
        assert token.access_token == "new"

    def test_raises_when_no_token_available(self, tmp_home: Path) -> None:
        with pytest.raises(sg_auth.AuthError):
            sg_auth.get_valid_token()

    def test_raises_when_expired_cached_token_cannot_refresh(
        self, tmp_home: Path
    ) -> None:
        expired = CachedToken(
            access_token="old",
            refresh_token=None,  # no refresh token
            expires_at=_past(60),
            endpoint="https://sourcegraph.com",
        )
        sg_auth.save_cached_token(expired)
        with pytest.raises(sg_auth.AuthError):
            sg_auth.get_valid_token()


# ---------------------------------------------------------------------------
# device_code_flow / refresh_token — stubs since Sourcegraph cloud PAT-only
# ---------------------------------------------------------------------------


class TestUnimplementedFlows:
    def test_device_code_flow_not_implemented(self) -> None:
        with pytest.raises(NotImplementedError):
            sg_auth.device_code_flow("https://sourcegraph.com")

    def test_refresh_token_returns_none_when_no_refresh_token(self) -> None:
        cached = CachedToken(
            access_token="t",
            refresh_token=None,
            expires_at=_past(60),
            endpoint="https://sourcegraph.com",
        )
        assert sg_auth.refresh_token(cached) is None


# ---------------------------------------------------------------------------
# Error messages must not leak tokens
# ---------------------------------------------------------------------------


class TestNoTokenLeakage:
    def test_autherror_does_not_contain_token(
        self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        expired = CachedToken(
            access_token="SUPER_SECRET_ABC123",
            refresh_token=None,
            expires_at=_past(60),
            endpoint="https://sourcegraph.com",
        )
        sg_auth.save_cached_token(expired)
        try:
            sg_auth.get_valid_token()
        except sg_auth.AuthError as e:
            assert "SUPER_SECRET_ABC123" not in str(e)
