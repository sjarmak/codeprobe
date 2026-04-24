"""Tests for ``codeprobe check-infra offline``.

Bead: r16-offline-check-infra. Pins acceptance criteria 1, 2, 4:

* Exit 0 when every configured backend's credential TTL exceeds the
  expected run duration (or has no expiration).
* Exit non-zero with a backend-named remediation message when the
  Bedrock session token TTL is shorter than the expected run duration.
* ``get_credential_ttl(backend_name)`` returns a ``timedelta`` or ``None``.

These tests use stubbed environment variables only — they never hit real
AWS, Azure, or GCP endpoints.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from click.testing import CliRunner

from codeprobe.cli.check_infra import check_infra
from codeprobe.net.credential_ttl import (
    KNOWN_BACKENDS,
    CredentialTTLError,
    get_credential_ttl,
)

# ---------------------------------------------------------------------------
# get_credential_ttl unit tests (AC #4)
# ---------------------------------------------------------------------------


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _clear_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "AWS_SESSION_EXPIRATION",
        "AWS_CREDENTIAL_EXPIRATION",
        "GOOGLE_APPLICATION_CREDENTIALS_TOKEN_EXPIRY",
        "AZURE_TOKEN_EXPIRES_ON",
    ):
        monkeypatch.delenv(var, raising=False)


def test_anthropic_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_creds(monkeypatch)
    assert get_credential_ttl("anthropic") is None


def test_openai_compat_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_creds(monkeypatch)
    assert get_credential_ttl("openai_compat") is None


def test_bedrock_reads_session_expiration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_creds(monkeypatch)
    now = datetime(2026, 4, 22, 12, 0, 0, tzinfo=UTC)
    expires = now + timedelta(hours=4)
    monkeypatch.setenv("AWS_SESSION_EXPIRATION", _iso(expires))

    ttl = get_credential_ttl("bedrock", now=now)
    assert ttl == timedelta(hours=4)


def test_bedrock_falls_back_to_credential_expiration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_creds(monkeypatch)
    now = datetime(2026, 4, 22, 12, 0, 0, tzinfo=UTC)
    expires = now + timedelta(minutes=30)
    monkeypatch.setenv("AWS_CREDENTIAL_EXPIRATION", _iso(expires))

    ttl = get_credential_ttl("bedrock", now=now)
    assert ttl == timedelta(minutes=30)


def test_bedrock_expired_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_creds(monkeypatch)
    now = datetime(2026, 4, 22, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setenv(
        "AWS_SESSION_EXPIRATION", _iso(now - timedelta(minutes=1))
    )

    ttl = get_credential_ttl("bedrock", now=now)
    assert ttl == timedelta(0)


def test_vertex_reads_token_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_creds(monkeypatch)
    now = datetime(2026, 4, 22, 12, 0, 0, tzinfo=UTC)
    expires = now + timedelta(hours=2)
    monkeypatch.setenv(
        "GOOGLE_APPLICATION_CREDENTIALS_TOKEN_EXPIRY", _iso(expires)
    )

    ttl = get_credential_ttl("vertex", now=now)
    assert ttl == timedelta(hours=2)


def test_azure_reads_token_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_creds(monkeypatch)
    now = datetime(2026, 4, 22, 12, 0, 0, tzinfo=UTC)
    expires = now + timedelta(minutes=45)
    monkeypatch.setenv("AZURE_TOKEN_EXPIRES_ON", _iso(expires))

    ttl = get_credential_ttl("azure_openai", now=now)
    assert ttl == timedelta(minutes=45)


def test_unknown_backend_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_creds(monkeypatch)
    with pytest.raises(CredentialTTLError):
        get_credential_ttl("not-a-real-backend")


def test_malformed_timestamp_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_creds(monkeypatch)
    monkeypatch.setenv("AWS_SESSION_EXPIRATION", "definitely not a timestamp")
    with pytest.raises(CredentialTTLError):
        get_credential_ttl("bedrock")


def test_known_backends_matches_registry_matrix() -> None:
    """KNOWN_BACKENDS must stay in sync with llm.backends.BACKEND_CLASSES."""
    from codeprobe.llm.backends import BACKEND_CLASSES

    assert set(KNOWN_BACKENDS) == set(BACKEND_CLASSES.keys())


# ---------------------------------------------------------------------------
# check-infra offline CLI tests (AC #1, #2)
# ---------------------------------------------------------------------------


def _plenty_of_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set Bedrock session to expire well after the default 1h run budget."""
    _clear_creds(monkeypatch)
    future = datetime.now(tz=UTC) + timedelta(hours=12)
    monkeypatch.setenv("AWS_SESSION_EXPIRATION", _iso(future))


def test_offline_exit_zero_when_ttls_exceed_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plenty_of_time(monkeypatch)

    result = CliRunner().invoke(
        check_infra,
        ["offline", "--expected-run-duration", "1h", "--no-json"],
    )

    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_offline_fails_when_bedrock_ttl_short_and_names_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #2: Bedrock TTL < expected duration fails with a backend-named
    remediation message."""
    _clear_creds(monkeypatch)
    # Set bedrock to expire in 10 minutes; we'll demand 1h.
    soon = datetime.now(tz=UTC) + timedelta(minutes=10)
    monkeypatch.setenv("AWS_SESSION_EXPIRATION", _iso(soon))

    result = CliRunner().invoke(
        check_infra,
        [
            "offline",
            "--expected-run-duration",
            "1h",
            "--backend",
            "bedrock",
        ],
    )

    assert result.exit_code != 0
    combined = result.output + (result.stderr_bytes or b"").decode(
        errors="replace"
    )
    # The failure must name the backend explicitly (AC #2) and carry a
    # remediation hint.
    assert "bedrock" in combined.lower()
    assert "remediation" in combined.lower()


def test_offline_reports_expired_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_creds(monkeypatch)
    past = datetime.now(tz=UTC) - timedelta(minutes=5)
    monkeypatch.setenv("AWS_SESSION_EXPIRATION", _iso(past))

    result = CliRunner().invoke(
        check_infra,
        [
            "offline",
            "--expected-run-duration",
            "30m",
            "--backend",
            "bedrock",
        ],
    )

    assert result.exit_code != 0
    combined = result.output + (result.stderr_bytes or b"").decode(
        errors="replace"
    )
    assert "EXPIRED" in combined
    assert "bedrock" in combined.lower()


def test_offline_noexpiry_backends_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_creds(monkeypatch)
    result = CliRunner().invoke(
        check_infra,
        [
            "offline",
            "--expected-run-duration",
            "2h",
            "--backend",
            "anthropic",
            "--no-json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "no-expiry" in result.output
    assert "anthropic" in result.output


def test_offline_unknown_backend_filter_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_creds(monkeypatch)
    result = CliRunner().invoke(
        check_infra,
        ["offline", "--backend", "my-fake-backend"],
    )
    assert result.exit_code != 0
    combined = result.output + (result.stderr_bytes or b"").decode(
        errors="replace"
    )
    assert "my-fake-backend" in combined


def test_offline_rejects_bogus_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_creds(monkeypatch)
    result = CliRunner().invoke(
        check_infra,
        ["offline", "--expected-run-duration", "not-a-duration"],
    )
    assert result.exit_code != 0


def test_offline_accepts_various_duration_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_creds(monkeypatch)
    for duration in ("30s", "15m", "2h", "1d"):
        result = CliRunner().invoke(
            check_infra,
            [
                "offline",
                "--expected-run-duration",
                duration,
                "--backend",
                "anthropic",
            ],
        )
        assert result.exit_code == 0, (
            f"duration {duration} failed: {result.output}"
        )
