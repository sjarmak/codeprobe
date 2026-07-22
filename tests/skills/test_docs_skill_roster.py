"""Doc-roster guardrail: user-facing docs may only name skills that ship.

Regression guard for codeprobe-f7rl.42: ``docs/workflows/with-agents.md``
once documented a phantom skill roster (``/experiment``,
``/assess-codebase``, ``/ratings``, plus internal names like
``mine-tasks`` and ``acceptance-loop``) and a ``cp -r`` install flow that
required a repository checkout. These tests parse the two user-facing
docs and pin them to the packaged reality: the five skills under
``src/codeprobe/skills_data/`` and the ``codeprobe skills install``
command.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DATA_DIR = REPO_ROOT / "src" / "codeprobe" / "skills_data"
WITH_AGENTS_DOC = REPO_ROOT / "docs" / "workflows" / "with-agents.md"
README_DOC = REPO_ROOT / "README.md"
DOC_PATHS = [WITH_AGENTS_DOC, README_DOC]

# Backticked spans are where docs name skills; a skill-name-shaped token is
# lowercase kebab-case, optionally slash-prefixed for the (banned) slash-
# invocation style.
BACKTICKED_TOKEN_RE = re.compile(r"`([^`\n]+)`")
SKILL_NAME_SHAPE_RE = re.compile(r"/?[a-z][a-z0-9]*(?:-[a-z0-9]+)*")

# Backticked ``codeprobe-*`` tokens that are NOT skill references. Kept
# explicit so a typo'd skill name cannot hide behind a loose filter.
NON_SKILL_CODEPROBE_TOKENS = {
    "codeprobe-ekhi",  # bead id cited in README's MCP-overlap note
}

# Skills the docs advertised but that never shipped (codeprobe-f7rl.42).
# Slash-prefixed forms are checked anywhere in the text; ``scaffold`` and
# ``probe`` are real CLI subcommands (``codeprobe scaffold``) and README
# legitimately documents ``--hide-local-source scaffold``, so their
# bare-backticked forms count as skill names only in with-agents.md.
PHANTOM_SLASH_RE = re.compile(
    r"(?<!\w)/(experiment|assess-codebase|ratings|scaffold|probe)\b"
)
PHANTOM_HYPHEN_NAMES = (
    "mine-tasks",
    "run-eval",
    "acceptance-loop",
    "integration-test",
)
PHANTOM_BACKTICKED_TOKENS = ("`scaffold`", "`probe`")


def _shipped_skills() -> set[str]:
    return {
        path.name
        for path in SKILLS_DATA_DIR.iterdir()
        if (path / "SKILL.md").is_file()
    }


def _referenced_skills(text: str) -> set[str]:
    """Extract skill names referenced in backticked tokens.

    Both reference styles the docs have used count: slash-prefixed
    (```/name```) and bare packaged names (```codeprobe-name```).
    """
    refs: set[str] = set()
    for raw in BACKTICKED_TOKEN_RE.findall(text):
        token = raw.strip()
        if not SKILL_NAME_SHAPE_RE.fullmatch(token):
            continue
        if token.startswith("/"):
            refs.add(token[1:])
        elif (
            token.startswith("codeprobe-")
            and token not in NON_SKILL_CODEPROBE_TOKENS
        ):
            refs.add(token)
    return refs


def _doc_id(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


@pytest.mark.parametrize("doc_path", DOC_PATHS, ids=_doc_id)
def test_referenced_skills_all_ship(doc_path: Path) -> None:
    """Every skill a doc names must exist under skills_data."""
    referenced = _referenced_skills(doc_path.read_text(encoding="utf-8"))
    missing = referenced - _shipped_skills()
    assert not missing, (
        f"{doc_path}: references skills that do not ship under "
        f"{SKILLS_DATA_DIR}: {sorted(missing)}"
    )


@pytest.mark.parametrize("doc_path", DOC_PATHS, ids=_doc_id)
def test_phantom_skill_names_absent(doc_path: Path) -> None:
    """The pre-f7rl.42 phantom roster must not reappear in any form."""
    text = doc_path.read_text(encoding="utf-8")
    hits: list[str] = []

    hits.extend(
        f"slash reference `{match.group(0)}`"
        for match in PHANTOM_SLASH_RE.finditer(text)
    )
    hits.extend(
        f"internal-skill name `{name}`"
        for name in PHANTOM_HYPHEN_NAMES
        if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text)
    )
    if doc_path == WITH_AGENTS_DOC:
        hits.extend(
            f"backticked skill token {token}"
            for token in PHANTOM_BACKTICKED_TOKENS
            if token in text
        )

    assert not hits, (
        f"{doc_path}: phantom skill references found:\n  - "
        + "\n  - ".join(hits)
    )


def test_with_agents_has_no_cp_r_install_flow() -> None:
    """The checkout-and-copy install flow is gone for good: it required a
    repo clone and, on a maintainer machine, swept untracked rig skills
    into the customer's repo."""
    text = WITH_AGENTS_DOC.read_text(encoding="utf-8")
    assert "cp -r" not in text, (
        f"{WITH_AGENTS_DOC}: still documents a `cp -r` install flow; the "
        f"supported path is `codeprobe skills install`"
    )


@pytest.mark.parametrize("doc_path", DOC_PATHS, ids=_doc_id)
def test_docs_name_skills_install(doc_path: Path) -> None:
    """Both user-facing docs must point at ``codeprobe skills install``."""
    text = doc_path.read_text(encoding="utf-8")
    assert "skills install" in text, (
        f"{doc_path}: must document the `codeprobe skills install` command"
    )
