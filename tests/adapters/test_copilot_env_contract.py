"""Copilot credential and provider environment boundaries."""

import pytest

from codeprobe.adapters._base import (
    _ADAPTER_ENV_WHITELIST,
    _CONTAINER_ENV_KEYS,
    _adapter_safe_env,
)

_COPILOT_ENV_KEYS = {
    "COPILOT_GITHUB_TOKEN",
    "GH_TOKEN",
    "COPILOT_OFFLINE",
    "COPILOT_PROVIDER_BASE_URL",
    "COPILOT_PROVIDER_API_KEY",
    "COPILOT_PROVIDER_TYPE",
    "COPILOT_MODEL",
}


def test_safe_env_admits_supported_copilot_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "github_pat_test")
    monkeypatch.setenv("GH_TOKEN", "github_pat_fallback")
    monkeypatch.setenv("COPILOT_PROVIDER_API_KEY", "provider-secret")
    monkeypatch.setenv("COPILOT_API_KEY", "obsolete-secret")

    env = _adapter_safe_env(None)

    assert env["COPILOT_GITHUB_TOKEN"] == "github_pat_test"
    assert env["GH_TOKEN"] == "github_pat_fallback"
    assert env["COPILOT_PROVIDER_API_KEY"] == "provider-secret"
    assert "COPILOT_API_KEY" not in env


def test_copilot_environment_contract_is_consistent() -> None:
    assert _COPILOT_ENV_KEYS <= _ADAPTER_ENV_WHITELIST
    assert _COPILOT_ENV_KEYS | {"GITHUB_TOKEN"} <= _CONTAINER_ENV_KEYS
    assert "COPILOT_API_KEY" not in _ADAPTER_ENV_WHITELIST
