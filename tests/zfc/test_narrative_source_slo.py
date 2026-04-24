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
    """Fail the build when the SLO has elapsed AND the ZFC debt is still listed.

    The refactor landed on 2026-04-24 via bead ``codeprobe-0vk``: the
    narrative-source resolver now delegates to ``core/llm.py`` under the
    fixed rubric ``_NARRATIVE_RUBRIC_V1`` and falls back to the legacy
    priority only when no LLM backend is available. This test is kept as
    a regression guard against the exact ZFC-debt entry being
    re-introduced under ``### Known violations``.
    """
    claude_md = _claude_md_path()
    text = claude_md.read_text(encoding="utf-8")

    # Only fail if the entry is re-introduced into the Known-violations
    # section specifically — a mention elsewhere (changelog, commit log
    # reference, docstring quote) is fine.
    known_section = text.split("### Known violations", 1)
    if len(known_section) == 2:
        # Look only inside the Known-violations block, up to the next
        # top-level section header.
        after = known_section[1].split("\n## ", 1)[0]
        after = after.split("\n### ", 1)[0]
        entry_regressed = ENTRY_MARKER in after
    else:
        entry_regressed = False

    today = date.today()

    if not entry_regressed:
        # ZFC debt has been paid down — test always passes.
        return

    if today < NARRATIVE_SOURCE_SLO_DEADLINE:
        pytest.skip(
            f"narrative-source priority is re-listed ZFC debt until "
            f"{NARRATIVE_SOURCE_SLO_DEADLINE.isoformat()}; "
            f"remove the entry from CLAUDE.md when refactored."
        )

    pytest.fail(
        f"SLO expired ({NARRATIVE_SOURCE_SLO_DEADLINE.isoformat()}): "
        f"the ZFC debt entry '{ENTRY_MARKER}' has been re-introduced "
        "under '### Known violations' in CLAUDE.md. The resolver must "
        "stay delegated to core/llm.py (see codeprobe-0vk)."
    )
