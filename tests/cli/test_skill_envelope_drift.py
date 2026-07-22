"""Blocking drift test: the envelope schemas documented in the
``.claude/skills/codeprobe-*/SKILL.md`` agent contracts must match the real
:class:`codeprobe.cli.envelope.Envelope` dataclass.

Mirrors ``tests/cli/test_error_codes_drift.py``: the skills previously
documented a fictional envelope (``status``/``errors[]``/``data.tasks[]``)
that never appears on stdout, so an agent following its own contract parsed
keys that did not exist (codeprobe-f7rl.16). This test scans every fenced
``json`` block that documents a terminal envelope — declared via
``"record_type": "envelope"`` or shaped like one (a top-level ``exit_code``
key) — and asserts:

1. every top-level key in the block is a field of the ``Envelope`` dataclass;
2. the forbidden legacy key ``status`` never appears at the top level;
3. the core agent-contract skills each document at least one envelope block
   (so the check cannot pass vacuously after a rewrite drops the section).

Top-level keys are found with a mechanical brace-depth scan rather than
``json.loads`` because the documented blocks legitimately contain
placeholders (``"<abs-path or null>"``) and elisions.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from codeprobe.cli.envelope import Envelope

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SKILLS_ROOT = _REPO_ROOT / ".claude" / "skills"

# Core agent contracts that MUST document the envelope. The drift check
# itself runs over every codeprobe-* skill found on disk.
_CORE_SKILL_NAMES: tuple[str, ...] = (
    "codeprobe-mine",
    "codeprobe-run",
    "codeprobe-interpret",
)


def _all_codeprobe_skill_files() -> list[Path]:
    return sorted(_SKILLS_ROOT.glob("codeprobe-*/SKILL.md"))

_ENVELOPE_FIELDS: frozenset[str] = frozenset(
    f.name for f in dataclasses.fields(Envelope)
)

_FORBIDDEN_TOP_LEVEL_KEYS: frozenset[str] = frozenset({"status"})

# ---------------------------------------------------------------------------
# Fenced-block scanning
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)

_ENVELOPE_MARKER_RE = re.compile(r'"record_type"\s*:\s*"envelope"')

# One pass over braces and quoted keys keeps them ordered so brace depth is
# correct when a key is seen. Quoted *values* are not followed by ``:`` and
# never match the key group.
_TOKEN_RE = re.compile(
    r'"(?P<key>[A-Za-z_][A-Za-z0-9_]*)"\s*:|(?P<brace>[{}\[\]])'
)


def _fenced_json_blocks(text: str) -> list[str]:
    """Return the bodies of all ```json fenced blocks in *text*."""
    return _FENCE_RE.findall(text)


def _top_level_keys(block: str) -> list[str]:
    """Return quoted keys at brace depth 1 (the envelope's own fields)."""
    keys: list[str] = []
    depth = 0
    for m in _TOKEN_RE.finditer(block):
        brace = m.group("brace")
        if brace in ("{", "["):
            depth += 1
        elif brace in ("}", "]"):
            depth -= 1
        elif m.group("key") is not None and depth == 1:
            keys.append(m.group("key"))
    return keys


def _is_envelope_block(block: str) -> bool:
    """True when the block documents a terminal envelope.

    Either it declares ``"record_type": "envelope"`` outright, or it is
    envelope-shaped: a top-level ``exit_code`` key (the pre-fix fictional
    schemas all carried one, so a rogue block cannot dodge the check by
    omitting ``record_type``). NDJSON event examples carry neither.
    """
    if _ENVELOPE_MARKER_RE.search(block):
        return True
    return "exit_code" in _top_level_keys(block)


def _envelope_blocks(text: str) -> list[str]:
    """Return only the fenced blocks that document a terminal envelope."""
    return [b for b in _fenced_json_blocks(text) if _is_envelope_block(b)]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("skill_name", _CORE_SKILL_NAMES)
def test_core_skill_documents_at_least_one_envelope_block(
    skill_name: str,
) -> None:
    """Each core agent-contract skill must carry an envelope schema block."""
    skill_md = _SKILLS_ROOT / skill_name / "SKILL.md"
    assert skill_md.is_file(), f"expected skill contract at {skill_md}"
    blocks = _envelope_blocks(skill_md.read_text(encoding="utf-8"))
    assert blocks, (
        f"{skill_md.relative_to(_REPO_ROOT)} documents no fenced json "
        "envelope block — the schema section is the agent contract and "
        "must not be dropped."
    )


def test_documented_envelope_keys_match_dataclass() -> None:
    """Every documented top-level key must be an Envelope dataclass field.

    Runs over every ``codeprobe-*`` skill on disk so a newly added skill
    cannot re-introduce the fictional ``status``/``errors[]`` schema.
    """
    skill_files = _all_codeprobe_skill_files()
    assert skill_files, f"no codeprobe-* skills found under {_SKILLS_ROOT}"

    problems: list[str] = []
    for skill_md in skill_files:
        rel = skill_md.relative_to(_REPO_ROOT)
        for i, block in enumerate(_envelope_blocks(skill_md.read_text()), 1):
            keys = _top_level_keys(block)
            unknown = [
                k
                for k in keys
                if k not in _ENVELOPE_FIELDS
                and k not in _FORBIDDEN_TOP_LEVEL_KEYS
            ]
            forbidden = [k for k in keys if k in _FORBIDDEN_TOP_LEVEL_KEYS]
            if unknown:
                problems.append(
                    f"{rel} block {i}: keys {unknown} are not Envelope "
                    f"fields (known: {sorted(_ENVELOPE_FIELDS)})"
                )
            if forbidden:
                problems.append(
                    f"{rel} block {i}: forbidden legacy key(s) {forbidden} "
                    "documented as envelope keys — the real envelope has "
                    "`ok`, not `status`."
                )
    assert not problems, "Envelope-schema drift:\n" + "\n".join(
        f"  - {p}" for p in problems
    )


def test_scanner_flags_fictional_envelope() -> None:
    """Negative test — the scan itself must catch the old fictional schema.

    The fictional block deliberately omits ``record_type``: the pre-fix
    mine/interpret schemas had none, so the shape heuristic (top-level
    ``exit_code``) must classify it as an envelope block on its own.
    """
    fictional = """\
# fake skill

```json
{
  "status": "ok",
  "command": "mine",
  "exit_code": 0,
  "errors": [ { "code": "<CODE>", "remediation": "..." } ]
}
```
"""
    blocks = _envelope_blocks(fictional)
    assert len(blocks) == 1
    keys = _top_level_keys(blocks[0])
    assert "status" in keys
    assert "errors" in keys
    assert "errors" not in _ENVELOPE_FIELDS
    # Nested keys (inside the errors array) must NOT surface as top-level.
    assert "code" not in keys
    assert "remediation" not in keys


def test_scanner_accepts_real_envelope_and_ignores_nested_status() -> None:
    """A data-level ``status`` (depth > 1) is legitimate and must pass."""
    real = """\
```json
{
  "record_type": "envelope",
  "ok": true,
  "command": "run",
  "version": "1.0.0",
  "schema_version": "1",
  "exit_code": 0,
  "data": { "results": [ { "task_id": "t1", "status": "done" } ] },
  "error": null,
  "warnings": [],
  "next_steps": []
}
```
"""
    blocks = _envelope_blocks(real)
    assert len(blocks) == 1
    keys = _top_level_keys(blocks[0])
    assert set(keys) <= _ENVELOPE_FIELDS
    assert "status" not in keys  # nested inside data, not top-level


def test_event_blocks_are_not_envelope_blocks() -> None:
    """NDJSON event examples must not be policed as envelopes."""
    event = """\
```json
{
  "record_type": "event",
  "event": "task_done",
  "task_id": "t1"
}
```
"""
    assert _envelope_blocks(event) == []
