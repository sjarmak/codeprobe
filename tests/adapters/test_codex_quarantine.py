"""CodexAdapter quarantine contract (codeprobe-f7rl.27, decision 4).

The codex adapter is a single-shot completion API, not a coding agent —
it never sees the workspace and cannot edit files. The ``quarantined``
class attribute is the single source of truth run preflight and
``experiment add-config`` refuse on.
"""

from __future__ import annotations

from codeprobe.adapters.claude import ClaudeAdapter
from codeprobe.adapters.codex import CodexAdapter
from codeprobe.adapters.copilot import CopilotAdapter


def test_codex_adapter_is_quarantined() -> None:
    assert CodexAdapter.quarantined is True


def test_comparison_adapters_are_not_quarantined() -> None:
    """claude and copilot must stay dispatchable — only codex is fenced."""
    for cls in (ClaudeAdapter, CopilotAdapter):
        assert getattr(cls, "quarantined", False) is False
