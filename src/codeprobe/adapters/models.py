"""Per-agent known-model registry — the single source of truth for valid
``--model`` tokens.

A static, structural allow-list (ZFC-OK: a lookup table, not a semantic
judgment). Three consumers share this one registry so no model list is
duplicated:

* ``codeprobe init`` wizard — supplies the default + example tokens and
  rejects an unknown entry at prompt time.
* ``codeprobe run`` preflight — rejects an unknown ``--model`` *before* any
  agent subprocess is spawned. Without this the bad token flows through to
  the agent CLI, which errors deep in the run; that error is then scored
  ``0.0`` and silently contaminates the comparison (codeprobe-fvfo Gap 1/2).
* ``codeprobe models list`` — prints the known tokens per agent.

Validation is enforced only for agents whose accepted token set we know
authoritatively (``validated=True``). For agents where the upstream model
set is fluid (codex, copilot), the entry is *advisory*: it seeds the wizard
default and ``models list`` output but never rejects a token, so a fast-moving
vendor model is not falsely blocked. The errored-run exclusion (codeprobe-h3j4)
is the backstop there.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Date-suffixed full IDs (claude-sonnet-4-6-20250514) normalize to their
# short form (claude-sonnet-4-6); both forms are accepted.
_DATE_SUFFIX = re.compile(r"-\d{8}$")


@dataclass(frozen=True)
class ModelSet:
    """Known model tokens for one agent.

    ``aliases`` maps a short alias (``sonnet``) to its canonical id
    (``claude-sonnet-4-6``). ``full_ids`` are the canonical short ids.
    ``id_pattern``, when set, accepts structurally-valid ids the enumerated
    list has not caught up to yet (a newer/older dated release) so a real
    model is never falsely rejected; egregiously wrong tokens (``opus-4``,
    ``gpt-4``) still fail because they match neither an alias, a known id,
    nor the vendor's id shape.
    """

    agent: str
    default: str
    aliases: dict[str, str]
    full_ids: tuple[str, ...]
    id_pattern: re.Pattern[str] | None = None
    validated: bool = True

    def resolve(self, token: str) -> str | None:
        """Return the canonical id for a known token, else ``None``."""
        t = token.strip()
        if not t:
            return None
        if t in self.aliases:
            return self.aliases[t]
        stripped = _DATE_SUFFIX.sub("", t)
        if stripped in self.full_ids:
            return stripped
        if self.id_pattern is not None and self.id_pattern.match(stripped):
            return stripped
        return None

    def is_known(self, token: str) -> bool:
        return self.resolve(token) is not None

    def known_tokens(self) -> list[str]:
        """Aliases first (friendliest), then canonical ids — for display."""
        return list(self.aliases.keys()) + list(self.full_ids)


_REGISTRY: dict[str, ModelSet] = {
    "claude": ModelSet(
        agent="claude",
        default="claude-sonnet-4-6",
        aliases={
            "sonnet": "claude-sonnet-4-6",
            "opus": "claude-opus-4-7",
            "haiku": "claude-haiku-4-5",
        },
        full_ids=("claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"),
        # claude-<family>-<major>[-<minor>], optional -YYYYMMDD already stripped.
        id_pattern=re.compile(r"^claude-(opus|sonnet|haiku)-\d+(-\d+)?$"),
        validated=True,
    ),
    # Advisory: seeds the wizard default + `models list`, never rejects.
    "codex": ModelSet(
        agent="codex",
        default="codex-latest",
        aliases={},
        full_ids=("codex-latest", "codex-mini-latest"),
        id_pattern=None,
        validated=False,
    ),
    "copilot": ModelSet(
        agent="copilot",
        default="",
        aliases={},
        full_ids=(),
        id_pattern=None,
        validated=False,
    ),
}


def model_set(agent: str) -> ModelSet | None:
    """Return the :class:`ModelSet` for ``agent`` (``None`` if unregistered)."""
    return _REGISTRY.get(agent)


def known_agents() -> list[str]:
    return list(_REGISTRY.keys())


def validate_model(agent: str, token: str | None) -> None:
    """Raise a :class:`PrescriptiveError` if ``token`` is not a known model.

    No-op when ``token`` is empty (model is optional — the agent uses its
    own default), when the agent is unregistered, or when the agent's set is
    advisory (``validated=False``). Validates *without* mutating the token:
    aliases and short ids pass through to the agent CLI unchanged.
    """
    if token is None or not token.strip():
        return
    ms = _REGISTRY.get(agent)
    if ms is None or not ms.validated:
        # Unknown backends are rejected by the run preflight (cli/run_cmd.py
        # UNKNOWN_BACKEND) — this function only validates model tokens.
        return
    if ms.is_known(token):
        return

    # Imported lazily: cli.errors pulls in the CLI layer, which imports
    # adapters — a module-level import here would be circular.
    from codeprobe.cli.errors import PrescriptiveError

    known = ", ".join(ms.known_tokens())
    raise PrescriptiveError(
        code="UNKNOWN_MODEL",
        message=(
            f"Unknown {agent} model {token!r}. "
            f"Known tokens: {known}. "
            f"Run `codeprobe models list --agent {agent}` to see all options."
        ),
        message_for_agent=(
            f"Model {token!r} is not a valid {agent} token. "
            f"Retry with one of: {known}."
        ),
        next_try_flag="--model",
        next_try_value=ms.default,
        terminal=True,
        detail={"agent": agent, "given": token, "known": ms.known_tokens()},
    )
