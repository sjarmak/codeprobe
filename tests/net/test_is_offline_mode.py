"""Tests for ``codeprobe.net.is_offline_mode`` helper.

The helper reads ``CODEPROBE_OFFLINE`` from the environment and returns
a bool. It is introduced alongside the ``codeprobe run --offline`` flag
so subsystem callers can opt in to offline-aware short-circuits later.
"""

from __future__ import annotations

import pytest

from codeprobe.net import is_offline_mode


def test_returns_false_when_env_var_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEPROBE_OFFLINE", raising=False)
    assert is_offline_mode() is False


def test_returns_false_when_env_var_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEPROBE_OFFLINE", "")
    assert is_offline_mode() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "On", "YeS"])
def test_truthy_values_return_true(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("CODEPROBE_OFFLINE", value)
    assert is_offline_mode() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "anything-else"])
def test_falsy_values_return_false(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("CODEPROBE_OFFLINE", value)
    assert is_offline_mode() is False


def test_whitespace_is_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEPROBE_OFFLINE", "  1  ")
    assert is_offline_mode() is True
