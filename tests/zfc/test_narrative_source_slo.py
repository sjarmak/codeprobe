"""ZFC SLO guard for the ``resolve_narrative_source`` priority rule.

The priority rule ``pr > commits > rfcs > issues`` is a semantic
tiebreaker tracked as ZFC debt in ``CLAUDE.md § Known violations``.
Before the deadline (``2026-10-23``) this test *skips* — it's a dormant
guard. On or after the deadline, it fails the build if the entry is
still present, forcing a refactor that delegates narrative-source
selection to the model (see `core/llm.py`).

If the entry has been removed from CLAUDE.md, the test passes silently
regardless of date.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

NARRATIVE_SOURCE_SLO_DEADLINE = date(2026, 10, 23)
ENTRY_MARKER = "config/defaults.py:resolve_narrative_source"


def _claude_md_path() -> Path:
    """Locate CLAUDE.md at the repo root.

    Walks upward from this file until we find CLAUDE.md or hit the
    filesystem root. Works both in-worktree and in a detached clone.
    """
    here = Path(__file__).resolve()
    for ancestor in (here, *here.parents):
        candidate = ancestor / "CLAUDE.md" if ancestor.is_dir() else ancestor.parent / "CLAUDE.md"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Could not locate CLAUDE.md")


def test_narrative_source_priority_slo() -> None:
    """Fail the build when the SLO has elapsed AND the ZFC debt is still listed."""
    claude_md = _claude_md_path()
    text = claude_md.read_text(encoding="utf-8")
    entry_present = ENTRY_MARKER in text

    today = date.today()

    if not entry_present:
        # ZFC debt has been paid down — test always passes.
        return

    if today < NARRATIVE_SOURCE_SLO_DEADLINE:
        pytest.skip(
            f"narrative-source priority is acknowledged ZFC debt until "
            f"{NARRATIVE_SOURCE_SLO_DEADLINE.isoformat()}; "
            f"remove the entry from CLAUDE.md when refactored."
        )

    pytest.fail(
        f"SLO expired ({NARRATIVE_SOURCE_SLO_DEADLINE.isoformat()}): "
        f"the ZFC debt entry '{ENTRY_MARKER}' is still in CLAUDE.md. "
        "Refactor resolve_narrative_source to delegate selection to a "
        "model via core/llm.py, then remove the entry."
    )


def test_claude_md_has_entry_today() -> None:
    """Document the current state: entry is present in CLAUDE.md (until refactored)."""
    claude_md = _claude_md_path()
    text = claude_md.read_text(encoding="utf-8")
    assert ENTRY_MARKER in text, (
        "narrative-source priority debt missing from CLAUDE.md; "
        "add it back under '### Known violations' or remove this test."
    )
