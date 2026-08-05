"""Mechanical truth checks for docs/adapters.md (codeprobe-f7rl.44).

The adapter guide is the customer extension path, so its mechanically
checkable claims are pinned to the codebase: every referenced source path
must exist, the MinimalAdapter example must be executable truth against the
real Protocol, the phantom adapter stays deleted, and documented AgentConfig
defaults are introspected from the dataclass rather than trusted.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from codeprobe.adapters.protocol import AgentAdapter, AgentConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "docs" / "adapters.md"


def _doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def _python_blocks(text: str) -> list[str]:
    return re.findall(r"```python\n(.*?)```", text, flags=re.DOTALL)


def test_every_referenced_repo_path_exists() -> None:
    """Each backticked src/codeprobe/... or tests/... path resolves on disk."""
    text = _doc_text()
    paths = set(re.findall(r"`((?:src/codeprobe|tests)/[^`]+)`", text))
    assert paths, "docs/adapters.md should reference source paths in backticks"
    missing = sorted(p for p in paths if not (REPO_ROOT / p.rstrip("/")).exists())
    assert not missing, f"docs/adapters.md references nonexistent paths: {missing}"


def test_minimal_adapter_example_is_executable_truth() -> None:
    """The MinimalAdapter fenced block execs and satisfies the real Protocol."""
    blocks = [b for b in _python_blocks(_doc_text()) if "class MinimalAdapter" in b]
    assert blocks, "docs/adapters.md must contain a MinimalAdapter example"
    namespace: dict[str, object] = {}
    # The block carries its own imports and closing assert; exec is the point.
    exec(compile(blocks[0], "docs/adapters.md:MinimalAdapter", "exec"), namespace)
    adapter_cls = namespace["MinimalAdapter"]
    assert isinstance(adapter_cls(), AgentAdapter)  # type: ignore[operator]


def test_phantom_adapter_not_mentioned() -> None:
    """The deleted phantom reference implementation must not reappear."""
    assert "aider" not in _doc_text().lower()


def test_agent_config_timeout_row_matches_code() -> None:
    """The documented timeout_seconds default equals the real dataclass default."""
    match = re.search(
        r"^\|\s*`timeout_seconds`\s*\|[^|]*\|\s*`(\d+)`\s*\|",
        _doc_text(),
        flags=re.MULTILINE,
    )
    assert match, "AgentConfig table must have a timeout_seconds row with a default"
    assert int(match.group(1)) == AgentConfig().timeout_seconds


def test_agent_config_table_covers_all_fields() -> None:
    """Every AgentConfig field appears in the doc (the 'all nine fields' claim)."""
    text = _doc_text()
    missing = [
        f.name for f in dataclasses.fields(AgentConfig) if f"`{f.name}`" not in text
    ]
    assert not missing, f"docs/adapters.md is missing AgentConfig fields: {missing}"


def test_timeout_precedence_and_sourcegraph_guardrail_are_documented() -> None:
    """Pin the operator-visible behavior added after the kubernetes pilot."""
    text = _doc_text()
    assert "--timeout" in text
    assert "time_limit_sec" in text
    assert "mcp__sourcegraph__evaluator" in text
