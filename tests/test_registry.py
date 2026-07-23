"""Tests for core/registry.py — agent adapter resolution."""

from __future__ import annotations

import pytest

from codeprobe.adapters.claude import ClaudeAdapter
from codeprobe.adapters.copilot import CopilotAdapter
from codeprobe.adapters.protocol import AgentAdapter
from codeprobe.core.registry import available, resolve


def test_resolve_claude():
    adapter = resolve("claude")
    assert isinstance(adapter, ClaudeAdapter)
    assert isinstance(adapter, AgentAdapter)


def test_resolve_copilot():
    adapter = resolve("copilot")
    assert isinstance(adapter, CopilotAdapter)
    assert isinstance(adapter, AgentAdapter)


def test_resolve_unknown_raises():
    with pytest.raises(KeyError, match="unknown-agent"):
        resolve("unknown-agent")


def test_available_includes_builtins():
    names = available()
    assert "claude" in names
    assert "copilot" in names
    assert names == sorted(names)


def test_resolve_returns_same_class_type():
    a = resolve("claude")
    b = resolve("claude")
    assert type(a) is type(b)


def test_resolve_codex_still_registered():
    """codeprobe-f7rl.27: quarantine is run-preflight policy, not registry
    removal — resolution keeps working so the refusal is prescriptive
    (ADAPTER_QUARANTINED) rather than a raw KeyError."""
    from codeprobe.adapters.codex import CodexAdapter

    adapter = resolve("codex")
    assert isinstance(adapter, CodexAdapter)
    assert isinstance(adapter, AgentAdapter)


def test_available_lists_codex():
    assert "codex" in available()
