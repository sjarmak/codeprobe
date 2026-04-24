"""Alignment tests between repo-committed SKILL.md files and the CLI surface.

These tests are the CI guardrail for the paired-skills contract: every skill
under ``.claude/skills/codeprobe-*/SKILL.md`` must

1. carry valid YAML frontmatter with required fields and length caps,
2. reference only CLI flags that actually exist in ``<cmd> --help``, and
3. reference only error codes present in ``src/codeprobe/cli/error_codes.json``.

No skips, no xfails — this is the CI enforcement layer.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from codeprobe.cli import main as codeprobe_main

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / ".claude" / "skills"
ERROR_CODES_PATH = REPO_ROOT / "src" / "codeprobe" / "cli" / "error_codes.json"

SKILL_PATTERN = "codeprobe-*/SKILL.md"

DESCRIPTION_MAX_CHARS = 1536
BODY_MAX_LINES = 500

# Subcommand extraction — map a skill directory name to the CLI path it wraps.
# The first token is always the top-level command; multi-word paths are handled
# (e.g., ``check-infra drift``) by scanning the invocation body itself.
SKILL_TO_PRIMARY_CMD: dict[str, tuple[str, ...]] = {
    "codeprobe-mine": ("mine",),
    "codeprobe-run": ("run",),
    "codeprobe-interpret": ("interpret",),
    "codeprobe-check-infra": ("check-infra",),
    "codeprobe-calibrate": ("calibrate",),
}


def _iter_skills() -> list[Path]:
    return sorted(SKILLS_DIR.glob(SKILL_PATTERN))


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter from body. Expects leading ``---`` fence."""
    if not text.startswith("---\n"):
        raise AssertionError("SKILL.md must start with '---' frontmatter fence")
    rest = text[len("---\n"):]
    end = rest.find("\n---\n")
    if end < 0:
        raise AssertionError("SKILL.md frontmatter missing closing '---' fence")
    fm_text = rest[:end]
    body = rest[end + len("\n---\n"):]
    return yaml.safe_load(fm_text), body


FENCE_RE = re.compile(r"```(?:[a-zA-Z0-9_-]*)\n(.*?)```", re.DOTALL)
CODEPROBE_LINE_RE = re.compile(r"^\s*codeprobe\s+(.+?)\s*(?:\\?)$", re.MULTILINE)
FLAG_RE = re.compile(r"(--[a-z][a-z0-9-]+)")


def _extract_codeprobe_invocations(body: str) -> list[str]:
    """Return a list of ``codeprobe ...`` command-lines found in fenced blocks.

    Only looks inside triple-backtick fences to avoid matching narrative prose.
    """
    lines: list[str] = []
    for block in FENCE_RE.findall(body):
        for match in CODEPROBE_LINE_RE.finditer(block):
            # Normalize: drop trailing backslash-newline continuations
            invocation = match.group(1)
            # Skip placeholder "codeprobe <cmd>" references without a subcommand
            invocation = invocation.strip()
            if not invocation:
                continue
            lines.append(invocation)
    return lines


def _extract_flags(invocation: str) -> list[str]:
    """Return every ``--flag`` token present in an invocation string."""
    return FLAG_RE.findall(invocation)


def _resolve_subcommand_path(
    primary: tuple[str, ...], invocation: str
) -> tuple[str, ...]:
    """Given an invocation and the skill's primary top-level command, return
    the full subcommand path (e.g., ``("check-infra", "drift")``).

    We take the primary command as ground truth for word 1, then consume any
    additional non-flag, non-angle-bracket tokens that come immediately after
    it (e.g., ``check-infra drift`` where ``drift`` is a known subcommand).
    """
    tokens = invocation.split()
    # Strip the primary command prefix if present as the first token.
    if tokens and tokens[0] == primary[0]:
        tokens = tokens[1:]
    path = list(primary)
    for tok in tokens:
        if tok.startswith("-") or tok.startswith("<") or tok.startswith("`"):
            break
        if re.fullmatch(r"[a-z][a-z0-9-]*", tok):
            path.append(tok)
        else:
            break
    return tuple(path)


def _run_help(cmd_path: tuple[str, ...]) -> str:
    """Invoke ``codeprobe <cmd-path> --help`` via Click's CliRunner."""
    runner = CliRunner()
    result = runner.invoke(codeprobe_main, [*cmd_path, "--help"])
    if result.exit_code != 0:
        # Multi-word paths may resolve to a group whose --help still exits 0;
        # a non-zero here genuinely means the path is wrong.
        raise AssertionError(
            f"`codeprobe {' '.join(cmd_path)} --help` exited "
            f"{result.exit_code}: {result.output}"
        )
    return result.output


def _load_error_codes() -> set[str]:
    data = json.loads(ERROR_CODES_PATH.read_text())
    return {entry["code"] for entry in data["codes"]}


VALID_CODES = _load_error_codes()

# A "code-shaped" token: starts with a capital letter, contains at least one
# underscore, length >= 5. This deliberately avoids false positives on words
# like "NDJSON", "JSON", "TTL", etc. that lack underscores.
CODE_SHAPED_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")


# ---------------------------------------------------------------------------
# Parametrized tests: one per discovered SKILL.md.
# ---------------------------------------------------------------------------


def _skill_id(path: Path) -> str:
    return path.parent.name


SKILL_PATHS = _iter_skills()


@pytest.mark.parametrize("skill_path", SKILL_PATHS, ids=_skill_id)
def test_frontmatter_fields(skill_path: Path) -> None:
    text = skill_path.read_text()
    fm, body = _split_frontmatter(text)

    assert isinstance(fm, dict), f"{skill_path}: frontmatter must be a mapping"
    assert "name" in fm, f"{skill_path}: frontmatter missing 'name'"
    assert "description" in fm, f"{skill_path}: frontmatter missing 'description'"
    assert "user-invocable" in fm, (
        f"{skill_path}: frontmatter missing 'user-invocable'"
    )

    name = fm["name"]
    assert isinstance(name, str) and name == skill_path.parent.name, (
        f"{skill_path}: name '{name}' must match directory '{skill_path.parent.name}'"
    )

    desc = fm["description"]
    assert isinstance(desc, str), f"{skill_path}: description must be a string"
    assert len(desc) <= DESCRIPTION_MAX_CHARS, (
        f"{skill_path}: description {len(desc)} chars exceeds "
        f"{DESCRIPTION_MAX_CHARS}"
    )
    # Trigger phrases: require the word "Triggers" or "Use this" appear.
    assert "Triggers on" in desc or "Use this" in desc, (
        f"{skill_path}: description must contain trigger phrases "
        f"('Triggers on ...' or 'Use this ...')"
    )

    assert fm["user-invocable"] is False, (
        f"{skill_path}: 'user-invocable' must be false (autonomous agent skill)"
    )

    body_line_count = body.count("\n")
    assert body_line_count <= BODY_MAX_LINES, (
        f"{skill_path}: body has {body_line_count} lines, exceeds "
        f"{BODY_MAX_LINES}"
    )


@pytest.mark.parametrize("skill_path", SKILL_PATHS, ids=_skill_id)
def test_cli_flag_alignment(skill_path: Path) -> None:
    """Every ``--flag`` appearing in a fenced codeprobe invocation must appear
    in the corresponding ``<cmd> --help`` output."""
    _fm, body = _split_frontmatter(skill_path.read_text())
    skill_id = skill_path.parent.name
    primary = SKILL_TO_PRIMARY_CMD.get(skill_id)
    assert primary is not None, (
        f"{skill_path}: skill '{skill_id}' has no registered CLI mapping in "
        f"SKILL_TO_PRIMARY_CMD"
    )

    invocations = _extract_codeprobe_invocations(body)
    assert invocations, (
        f"{skill_path}: no fenced `codeprobe <cmd> ...` invocations found"
    )

    help_cache: dict[tuple[str, ...], str] = {}
    mismatches: list[str] = []

    for invocation in invocations:
        # Ignore preflight directives for OTHER commands (e.g. a mine SKILL
        # invoking ``codeprobe doctor --json`` as environment prep should still
        # be validated against the doctor command's help surface).
        cmd_path = _resolve_subcommand_path(primary, invocation)
        # If the invocation doesn't target this skill's primary command, we
        # validate it against its own help surface. This catches references to
        # sibling commands (doctor, check-infra) in Environment blocks.
        tokens = invocation.split()
        if tokens and tokens[0] != primary[0]:
            # Only validate if the first token is a top-level codeprobe cmd.
            root_help = help_cache.setdefault(
                (), _run_help(())
            )
            if tokens[0] not in root_help:
                # Not a codeprobe subcommand reference; skip.
                continue
            cmd_path = _resolve_subcommand_path((tokens[0],), invocation)

        flags = _extract_flags(invocation)
        if not flags:
            continue
        help_text = help_cache.setdefault(cmd_path, _run_help(cmd_path))
        for flag in flags:
            if flag not in help_text:
                mismatches.append(
                    f"  - `{flag}` in invocation `codeprobe {invocation}` "
                    f"not found in `codeprobe {' '.join(cmd_path)} --help`"
                )

    assert not mismatches, (
        f"{skill_path}: flag-alignment mismatches:\n" + "\n".join(mismatches)
    )


@pytest.mark.parametrize("skill_path", SKILL_PATHS, ids=_skill_id)
def test_error_codes_present_in_catalog(skill_path: Path) -> None:
    """Every code-shaped token in the SKILL.md must exist in
    ``src/codeprobe/cli/error_codes.json``.

    We use a conservative regex (must include at least one underscore) so
    that SCREAMING headings without underscores (e.g., ``NDJSON``, ``JSON``,
    ``TTL``) don't produce false positives.
    """
    _fm, body = _split_frontmatter(skill_path.read_text())
    # Allowlist of non-error-code constants that happen to match the shape.
    # Kept small and explicit — prefer extending this list over loosening the
    # regex.
    allowlist = {
        "RUBRIC_V1",
        "CODEPROBE_JSON",
        "CODEPROBE_MAX_COST_USD",
        "CODEPROBE_PARALLEL",
        "CODEPROBE_OFFLINE",
        "CODEPROBE_TENANT",
        "SOURCEGRAPH_ACCESS_TOKEN",
        "SIGINT_SIGTERM",
        "SIGINT_OR_SIGTERM",
    }

    unknown: list[str] = []
    for token in CODE_SHAPED_RE.findall(body):
        if token in VALID_CODES:
            continue
        if token in allowlist:
            continue
        unknown.append(token)

    assert not unknown, (
        f"{skill_path}: found code-shaped tokens not in error_codes.json and "
        f"not in allowlist:\n  - " + "\n  - ".join(sorted(set(unknown)))
    )


def test_required_preflight_directive_in_mine() -> None:
    """codeprobe-mine must include the literal `!`codeprobe doctor --json``
    environment directive per PRD §7.1 (pre-loads doctor state into prompt)."""
    path = SKILLS_DIR / "codeprobe-mine" / "SKILL.md"
    body = path.read_text()
    assert "!`codeprobe doctor --json`" in body, (
        f"{path}: missing literal `!`codeprobe doctor --json`` preflight directive"
    )


def test_run_skill_documents_dual_surface() -> None:
    """codeprobe-run must document the NDJSON+envelope dual-surface per
    PRD §7.2 + §5.4 (record_type events + terminal envelope)."""
    path = SKILLS_DIR / "codeprobe-run" / "SKILL.md"
    body = path.read_text()
    # Case-insensitive sanity checks on the key terms.
    lower = body.lower()
    for needle in ("ndjson", "record_type", "envelope", "--json"):
        assert needle.lower() in lower, (
            f"{path}: missing dual-surface marker '{needle}'"
        )


def test_terminal_errors_are_called_out() -> None:
    """Every SKILL.md must call out the max-retry depth of 2 and mark terminal
    errors as non-retryable."""
    for path in SKILL_PATHS:
        text = path.read_text().lower()
        assert "maximum retry depth" in text or "max retry" in text, (
            f"{path}: retry policy must state a maximum retry depth"
        )
        assert "2" in text, f"{path}: retry policy must mention the numeric cap 2"
        assert "terminal" in text, (
            f"{path}: retry policy must call out terminal errors"
        )


def test_discovers_five_skills() -> None:
    """Sanity check: we expect exactly the five PRD-specified skills."""
    discovered = {p.parent.name for p in SKILL_PATHS}
    expected = {
        "codeprobe-mine",
        "codeprobe-run",
        "codeprobe-interpret",
        "codeprobe-check-infra",
        "codeprobe-calibrate",
    }
    missing = expected - discovered
    assert not missing, f"Missing PRD-specified skills: {sorted(missing)}"
