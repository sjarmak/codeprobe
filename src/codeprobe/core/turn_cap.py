"""Task-category-aware ``max_turns`` resolution.

Resolves the per-trial turn cap from an explicit precedence ladder:
explicit config cap (CLI ``--max-turns`` or experiment.json) > task
``max_turns_override`` > per-family default > global default fallback.

Turn caps are a resource budget — the same class of policy knob as
timeouts and cost ceilings — so this is mechanism, not semantic judgment
(ZFC-allowed). The family is read from explicit task-metadata fields the
author already set; no heuristic classification happens here.

Origin: ``codeprobe-aupz`` showed a single global ``--max-turns=50``
collapses SDLC reward (87% of trials hit ``error_max_turns``) while being
harmless for ``oracle_checks`` (those finish in <10 turns). The 4cl6
retune then confirmed every finite SDLC cap depresses reward, so SDLC is
left uncapped here.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Per-family turn-cap defaults. ``None`` means uncapped — the per-task
#: subprocess timeout remains the only bound. SDLC tasks edit 3-5
#: interrelated files and need the room; ``oracle_checks`` tasks are narrow
#: and 50 is generous. Families absent from this table fall back to
#: :data:`DEFAULT_FAMILY_MAX_TURNS`.
FAMILY_DEFAULT_MAX_TURNS: dict[str, int | None] = {
    "sdlc": None,
    "oracle_checks": 50,
}

#: Fallback cap for tasks whose family is not in
#: :data:`FAMILY_DEFAULT_MAX_TURNS`. A finite, generous default so an
#: unrecognised family still runs bounded rather than relying on the
#: timeout alone.
DEFAULT_FAMILY_MAX_TURNS: int = 75

#: Canonical set of values for :attr:`TurnCapDecision.source`.
TURN_CAP_SOURCES: frozenset[str] = frozenset(
    {"cli", "experiment", "task", "family_default"}
)


@dataclass(frozen=True)
class TurnCapDecision:
    """The resolved turn cap plus the precedence rung that produced it.

    ``max_turns`` is ``None`` when the chosen rung is uncapped (e.g. the
    SDLC family default). ``source`` records which rung won so the trial
    envelope can surface it for cap-retune analysis.
    """

    max_turns: int | None
    source: str


def resolve_turn_cap_family(metadata_block: dict, verification: dict) -> str:
    """Map explicit task-metadata fields to a turn-cap family key.

    ``oracle_checks`` tasks declare ``oracle_checks`` in their verification
    ``type``/``reward_type``; SDLC and other tasks carry
    ``metadata.category``. This is a mechanical field lookup, not a
    semantic classification — the author already set these fields at mine
    time.

    Returns the family key (e.g. ``"sdlc"``, ``"oracle_checks"``) or an
    empty string when no family signal is present.
    """
    if (
        verification.get("reward_type") == "oracle_checks"
        or verification.get("type") == "oracle_checks"
    ):
        return "oracle_checks"
    category = metadata_block.get("category")
    if isinstance(category, str) and category:
        return category
    return ""


def resolve_turn_cap(
    *,
    config_max_turns: int | None,
    config_source: str,
    task_override: int | None,
    family: str,
) -> TurnCapDecision:
    """Resolve the effective per-trial turn cap.

    Precedence, highest first:

    1. Explicit config cap (CLI ``--max-turns`` or experiment.json). When
       set, ``config_source`` (``"cli"`` or ``"experiment"``) records which.
    2. Task ``max_turns_override``.
    3. Per-family default — may be ``None`` (uncapped, e.g. SDLC).
    4. Global default fallback (:data:`DEFAULT_FAMILY_MAX_TURNS`) for any
       family not present in :data:`FAMILY_DEFAULT_MAX_TURNS`.

    Rungs 3 and 4 both report ``source="family_default"`` — both are the
    family-default tier, the only difference being whether the family had
    an explicit entry.
    """
    if config_max_turns is not None:
        return TurnCapDecision(config_max_turns, config_source or "experiment")
    if task_override is not None:
        return TurnCapDecision(task_override, "task")
    if family in FAMILY_DEFAULT_MAX_TURNS:
        return TurnCapDecision(FAMILY_DEFAULT_MAX_TURNS[family], "family_default")
    return TurnCapDecision(DEFAULT_FAMILY_MAX_TURNS, "family_default")
