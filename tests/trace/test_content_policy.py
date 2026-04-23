"""Tests for codeprobe.trace.content_policy — env/auth/glob redaction."""

from __future__ import annotations

import pytest

from codeprobe.trace.content_policy import (
    REDACTED_AUTH,
    REDACTED_ENV,
    REDACTED_GLOB,
    ContentPolicy,
)


@pytest.mark.unit
def test_env_value_stripped() -> None:
    policy = ContentPolicy(env_values=frozenset({"super-secret-value"}))
    out = policy.apply("my token is super-secret-value!")
    assert out == f"my token is {REDACTED_ENV}!"


@pytest.mark.unit
def test_env_values_below_min_length_not_redacted() -> None:
    """The ``ContentPolicy`` constructor drops values <8 chars (handled by snapshot).
    When a short value is explicitly passed, it IS replaced — this test ensures
    the filter is applied by the default factory, not inside ``apply``."""
    import os

    saved = dict(os.environ)
    os.environ.clear()
    os.environ["SHORT"] = "xy"          # too short → filtered
    os.environ["LONGONE"] = "abcdefgh"  # >=8 chars → included
    try:
        policy = ContentPolicy()
        assert "xy" not in policy.env_values
        assert "abcdefgh" in policy.env_values
    finally:
        os.environ.clear()
        os.environ.update(saved)


@pytest.mark.unit
def test_authorization_header_redacted() -> None:
    policy = ContentPolicy(env_values=frozenset())
    out = policy.apply("Authorization: Bearer abc.def.ghij1234")
    assert REDACTED_AUTH in out
    assert "Bearer abc.def.ghij1234" not in out


@pytest.mark.unit
def test_x_api_key_redacted() -> None:
    policy = ContentPolicy(env_values=frozenset())
    out = policy.apply("X-Api-Key: my-secret-key-longvalue")
    assert REDACTED_AUTH in out
    assert "my-secret-key-longvalue" not in out


@pytest.mark.unit
def test_aws_session_token_redacted() -> None:
    policy = ContentPolicy(env_values=frozenset())
    # Synthetic fixture string — the whole point of this test is to verify
    # redaction. gitleaks:allow
    fake_token = "FwoGZXIvYXdz" + "EDsaDD" + "123"  # gitleaks:allow
    out = policy.apply(f"aws_session_token={fake_token}")
    assert REDACTED_AUTH in out
    assert fake_token not in out


@pytest.mark.unit
def test_gcp_bearer_redacted() -> None:
    policy = ContentPolicy(env_values=frozenset())
    # Synthetic GCP-shaped token for redaction test. gitleaks:allow
    fake_gcp = "ya29." + "a0AfH6SMBxyz" + "1234567890abcdef"  # gitleaks:allow
    out = policy.apply(f"token: {fake_gcp}")
    assert REDACTED_AUTH in out
    assert fake_gcp not in out


@pytest.mark.unit
def test_deny_glob_matches_tool_output() -> None:
    policy = ContentPolicy(
        env_values=frozenset(), deny_globs=("*/etc/passwd*",)
    )
    out = policy.apply("/etc/passwd:root:x:0:0:", is_output=True)
    assert out == REDACTED_GLOB


@pytest.mark.unit
def test_deny_glob_does_not_fire_on_input() -> None:
    """Globs apply to tool output only — input strings skip the glob pass."""
    policy = ContentPolicy(env_values=frozenset(), deny_globs=("*secret*",))
    out = policy.apply("has secret here", is_output=False)
    # env and auth passes don't hit, glob is gated off → unchanged
    assert out == "has secret here"


@pytest.mark.unit
def test_none_passthrough() -> None:
    policy = ContentPolicy(env_values=frozenset())
    assert policy.apply(None) is None


@pytest.mark.unit
def test_empty_string_passthrough() -> None:
    policy = ContentPolicy(env_values=frozenset())
    assert policy.apply("") == ""


@pytest.mark.unit
def test_multiple_env_values_redacted() -> None:
    policy = ContentPolicy(
        env_values=frozenset({"secret-alpha-123", "secret-beta-456"})
    )
    out = policy.apply("alpha=secret-alpha-123 and beta=secret-beta-456")
    assert "secret-alpha-123" not in out
    assert "secret-beta-456" not in out
    assert out.count(REDACTED_ENV) == 2
