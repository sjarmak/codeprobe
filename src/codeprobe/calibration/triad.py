"""Calibration triad — null/golden/adversarial fixture gates per task.

The triad is a property test for a task's scoring rubric. For each task
we score three synthesised agent outputs and assert each one's reward
falls within a band that the rubric is supposed to enforce:

* **Null** (no-op output) → reward ≤ 0.1
* **Golden** (the expected solution from ground_truth) → reward ≥ 0.9
* **Adversarial** (golden tokens diluted with distractors) → reward ≤ 0.5

A band breach is informative, not necessarily a defect: a recall-tilted
``oracle_overlap_recall`` task is designed to accept over-shipping, so an
adversarial dump *will* score 1.0 — and that's exactly the kind of finding
the calibration triad surfaces. Each band check is independent; ``all_pass``
is the conjunction.

The fixtures are synthesised from the task's ``ground_truth.json`` (or
``answer.txt`` for mined org-scale tasks) so the triad runs offline with
no LLM calls. The scoring path is the same one the executor uses at run
time, including the unified ScoreResult ``reward`` field.

The module is ZFC-compliant — no semantic judgement, just deterministic
fixture synthesis + the existing scoring rubric. The band thresholds and
distractor set are documented constants, not tuned on each invocation.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Literal

from codeprobe.core.scoring import (
    _COPYTREE_IGNORE,
    BinaryScorer,
    ScoreResult,
    get_scorer,
)
from codeprobe.qa.verify import (
    _extract_oracle_tokens,
    _read_ground_truth,
    load_task_meta,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BAND_LIMITS",
    "FAMILIES",
    "Family",
    "FixtureOutcome",
    "TriadResult",
    "discover_calibration_tasks",
    "run_triad",
    "synthesize_adversarial_output",
    "synthesize_golden_output",
    "synthesize_null_output",
]

Family = Literal["null", "golden", "adversarial"]

FAMILIES: tuple[Family, ...] = ("null", "golden", "adversarial")

# (lower, upper) reward bands. Inclusive on both ends. The bands come from
# the codeprobe-9fri bead and are the contract any task scoring rubric is
# expected to honour. Failures are surfaced, not silently smoothed.
BAND_LIMITS: dict[Family, tuple[float, float]] = {
    "null": (0.0, 0.1),
    "golden": (0.9, 1.0),
    "adversarial": (0.0, 0.5),
}

# Distractor pool used to dilute the adversarial fixture. The set is
# deliberately deterministic — a calibration triad must produce the same
# reward on every run for the same task. ~80 entries puts precision
# under ~20% even for a 20-file oracle, which keeps F1 below the 0.5 cap
# the bead specifies as the adversarial ceiling. The names are
# obviously-fake (``_unrelated`` prefix) so they can't accidentally match
# any real repo file.
_ADVERSARIAL_DISTRACTORS: tuple[str, ...] = tuple(
    f"src/_unrelated/component_{i:03d}.py" for i in range(20)
) + tuple(
    f"tests/_unrelated/test_module_{i:03d}.py" for i in range(20)
) + tuple(
    f"docs/_unrelated/note_{i:03d}.md" for i in range(10)
) + tuple(
    f"internal/_unrelated/util_{i:03d}.py" for i in range(10)
) + tuple(
    f"vendor/_unrelated/lib_{i:03d}.go" for i in range(10)
) + tuple(
    f"scripts/_unrelated/script_{i:03d}.sh" for i in range(10)
)

# Directories that look like task corpora when probed via discovery. The
# default roots cover the in-repo fixture sets the bead asks the triad to
# run against; callers can pass additional roots explicitly.
_DEFAULT_TASK_ROOTS: tuple[str, ...] = (
    "examples/dual",
    ".codeprobe/tasks",
    "e2e-codeprobe-self/tasks",
    "tests/fixtures",
)


@dataclass(frozen=True)
class FixtureOutcome:
    """Result of scoring one fixture (null/golden/adversarial) against a task."""

    family: Family
    agent_output: str
    reward: float
    score: float
    passed: bool
    scorer_family: str
    sub_scores: dict
    ir_metrics: dict
    diagnostics: dict
    error: str | None
    band: tuple[float, float]
    band_pass: bool
    duration_seconds: float

    def to_dict(self) -> dict:
        return {
            "family": self.family,
            "agent_output_preview": _truncate(self.agent_output, 400),
            "reward": self.reward,
            "score": self.score,
            "passed": self.passed,
            "scorer_family": self.scorer_family,
            "sub_scores": dict(self.sub_scores),
            "ir_metrics": dict(self.ir_metrics),
            "diagnostics": dict(self.diagnostics),
            "error": self.error,
            "band": list(self.band),
            "band_pass": self.band_pass,
            "duration_seconds": round(self.duration_seconds, 4),
        }


@dataclass(frozen=True)
class TriadResult:
    """All three fixture outcomes for one task plus task identity."""

    task_id: str
    task_dir: Path
    reward_type: str
    declared_scorer_family: str | None
    fixtures: tuple[FixtureOutcome, ...]
    error: str | None = None

    @property
    def all_pass(self) -> bool:
        return self.error is None and all(f.band_pass for f in self.fixtures)

    @property
    def fixtures_by_family(self) -> dict[Family, FixtureOutcome]:
        return {f.family: f for f in self.fixtures}

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "task_dir": str(self.task_dir),
            "reward_type": self.reward_type,
            "declared_scorer_family": self.declared_scorer_family,
            "fixtures": [f.to_dict() for f in self.fixtures],
            "all_pass": self.all_pass,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Fixture synthesis
# ---------------------------------------------------------------------------


def synthesize_null_output(task_dir: Path) -> str:
    """Generate the null fixture output: empty string."""
    del task_dir  # unused; kept for symmetry
    return ""


def synthesize_golden_output(task_dir: Path) -> str:
    """Derive the golden agent output from the task's ground truth.

    Resolution order — ground_truth.json wins over a shipped ``answer.txt``
    because mined answer.txt files can drift out of sync with the oracle
    (we have observed cases where a manual edit left a stale answer.txt
    against an updated ground_truth). The ground truth is the contract;
    ``answer.txt`` is a hint for the offline runner only.

    1. ``tests/ground_truth.json`` (or ``ground_truth.json``):
       - v1 ``answer`` (scalar or list)
       - legacy ``expected`` list
       - v2 ``checks[*].answer`` — concatenate first list-style answer or
         first scalar (one fixture, one answer).
    2. ``answer.txt`` at the task root — used only when the ground truth
       cannot supply an answer (e.g. validation-style tasks where the
       oracle is purely behavioural).
    3. Empty string fallback. Tasks that fall through here will trip the
       golden band (≥ 0.9) and surface as follow-ups, which is the desired
       signal — the rubric cannot be calibrated against a missing oracle.
    """
    gt = _read_ground_truth(task_dir)
    if "answer" in gt:
        return _stringify_answer(gt["answer"])
    if isinstance(gt.get("expected"), list):
        return "\n".join(str(x) for x in gt["expected"] if x is not None)
    if isinstance(gt.get("checks"), list):
        for check in gt["checks"]:
            if not isinstance(check, dict):
                continue
            if "answer" in check:
                return _stringify_answer(check["answer"])

    answer_txt = task_dir / "answer.txt"
    if answer_txt.is_file():
        try:
            text = answer_txt.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if text:
            return text
    return ""


def synthesize_adversarial_output(task_dir: Path) -> str:
    """Generate the adversarial fixture: ground-truth tokens diluted with noise.

    The output prepends ``_ADVERSARIAL_DISTRACTORS`` (deterministic noise)
    to the oracle tokens. This depresses precision while preserving recall,
    so F1-tilted families fall under the 0.5 ceiling while recall-tilted
    families still over-score — exactly the signal the triad wants to expose.
    """
    tokens = _extract_oracle_tokens(_read_ground_truth(task_dir))
    if not tokens:
        # Without oracle tokens to echo, the adversarial fixture devolves
        # into pure noise — equivalent to null but with extra entropy.
        # That's fine for the band check (still ≤ 0.5) and avoids a
        # fragile branch downstream.
        return "\n".join(_ADVERSARIAL_DISTRACTORS)
    return "\n".join([*_ADVERSARIAL_DISTRACTORS, *tokens])


def _stringify_answer(answer: object) -> str:
    """Coerce a ground_truth ``answer`` value into agent-output form."""
    if isinstance(answer, list):
        return "\n".join(str(x) for x in answer if x is not None)
    if isinstance(answer, bool):
        return "true" if answer else "false"
    return str(answer)


# ---------------------------------------------------------------------------
# Scorer selection + sandbox preparation
# ---------------------------------------------------------------------------


def _resolve_reward_type(meta_flat: dict) -> str:
    """Pick the reward_type the task is configured to use.

    Matches the executor's auto-detection: ``verification.reward_type``
    when present, else ``binary``. Org-scale tasks store it under their
    flattened metadata; ``load_task_meta`` flattens both TOML and JSON.
    """
    rt = meta_flat.get("reward_type")
    if isinstance(rt, str) and rt:
        return rt
    return "binary"


def _select_scorer(reward_type: str) -> object:
    """Resolve a scorer instance for ``reward_type``.

    Falls back to ``BinaryScorer`` for unknown types — calibration must
    not crash on a malformed task; the resulting scores will be uniformly
    low and the operator sees the failure pattern.
    """
    try:
        return get_scorer(reward_type)
    except (ValueError, KeyError):
        logger.warning(
            "Unknown reward_type %r — falling back to BinaryScorer", reward_type
        )
        return BinaryScorer()


@contextmanager
def _scratched_task_dir(task_dir: Path) -> Iterator[Path]:
    """Yield a temp copy of ``task_dir`` with ``answer.txt`` stripped.

    Required for the calibration triad: many mined tasks ship a golden
    ``answer.txt`` whose oracle.py prefers it over ``$AGENT_OUTPUT``. We
    must remove that file so the fixture's actual agent output is what
    the scorer evaluates.

    Two layers of copying happen here (this scratch dir + the scorer's
    inner sandbox), which is wasteful but isolates the calibration code
    from the scoring internals — no upstream changes to ``_run_in_sandbox``.
    """
    scratch = Path(tempfile.mkdtemp(prefix="codeprobe-triad-scratch-"))
    try:
        scratch_task = scratch / task_dir.name
        shutil.copytree(
            task_dir,
            scratch_task,
            symlinks=False,
            ignore=shutil.ignore_patterns(*_COPYTREE_IGNORE),
        )
        ans = scratch_task / "answer.txt"
        if ans.exists():
            try:
                ans.unlink()
            except OSError:
                pass
        yield scratch_task
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def run_triad(task_dir: Path) -> TriadResult:
    """Run all three fixtures against ``task_dir`` and return the outcome.

    The function never raises on per-task failure — task-level errors are
    captured into ``TriadResult.error`` so a corpus run can table the
    surviving rows without losing the failed ones.
    """
    task_dir = Path(task_dir)
    task_id = task_dir.name

    if not task_dir.is_dir():
        return TriadResult(
            task_id=task_id,
            task_dir=task_dir,
            reward_type="",
            declared_scorer_family=None,
            fixtures=(),
            error=f"task directory does not exist: {task_dir}",
        )

    meta_flat = load_task_meta(task_dir)
    reward_type = _resolve_reward_type(meta_flat)
    declared_family = meta_flat.get("scorer_family")
    declared_family_str = (
        declared_family if isinstance(declared_family, str) and declared_family else None
    )

    scorer = _select_scorer(reward_type)

    fixtures: list[FixtureOutcome] = []
    for family in FAMILIES:
        outcome = _score_fixture(family, task_dir, scorer)
        fixtures.append(outcome)

    return TriadResult(
        task_id=task_id,
        task_dir=task_dir,
        reward_type=reward_type,
        declared_scorer_family=declared_family_str,
        fixtures=tuple(fixtures),
    )


def _score_fixture(
    family: Family, task_dir: Path, scorer: object
) -> FixtureOutcome:
    """Score one fixture and band-check its reward."""
    if family == "null":
        agent_output = synthesize_null_output(task_dir)
    elif family == "golden":
        agent_output = synthesize_golden_output(task_dir)
    elif family == "adversarial":
        agent_output = synthesize_adversarial_output(task_dir)
    else:  # pragma: no cover — unreachable under Literal type
        raise ValueError(f"unknown fixture family: {family!r}")

    band = BAND_LIMITS[family]
    start = monotonic()
    try:
        with _scratched_task_dir(task_dir) as scratch:
            result: ScoreResult = scorer.score(agent_output, scratch)  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover — defensive
        return FixtureOutcome(
            family=family,
            agent_output=agent_output,
            reward=0.0,
            score=0.0,
            passed=False,
            scorer_family="",
            sub_scores={},
            ir_metrics={},
            diagnostics={},
            error=f"scorer raised: {type(exc).__name__}: {exc}",
            band=band,
            band_pass=False,
            duration_seconds=monotonic() - start,
        )
    duration = monotonic() - start

    reward = float(result.reward) if result.reward is not None else float(result.score)
    band_pass = band[0] <= reward <= band[1]
    return FixtureOutcome(
        family=family,
        agent_output=agent_output,
        reward=reward,
        score=float(result.score),
        passed=bool(result.passed),
        scorer_family=str(result.scorer_family or ""),
        sub_scores=dict(result.sub_scores) if result.sub_scores else {},
        ir_metrics=dict(result.ir_metrics) if result.ir_metrics else {},
        diagnostics=dict(result.diagnostics) if result.diagnostics else {},
        error=result.error,
        band=band,
        band_pass=band_pass,
        duration_seconds=duration,
    )


# ---------------------------------------------------------------------------
# Task discovery
# ---------------------------------------------------------------------------


def discover_calibration_tasks(
    roots: Iterable[Path] | None = None,
) -> list[Path]:
    """Walk ``roots`` and return every directory that looks like a task.

    A task directory has at least one of ``task.toml`` / ``metadata.json``
    AND a ``tests/test.sh`` (the verification script the scorer runs).
    The walk is shallow-first: ``roots`` are inspected for direct task
    dirs, then suite-style subdirectories one level deeper. Anything
    nested under a discovered task dir is *not* re-scanned (avoids picking
    up worktree copies under ``.claude/worktrees``).

    Passing ``None`` uses the in-repo defaults (examples/, .codeprobe/,
    e2e-codeprobe-self/, tests/fixtures).
    """
    if roots is None:
        repo = _detect_repo_root()
        candidate_roots = [repo / r for r in _DEFAULT_TASK_ROOTS]
    else:
        candidate_roots = [Path(r) for r in roots]

    found: list[Path] = []
    seen: set[Path] = set()
    for root in candidate_roots:
        if not root.is_dir():
            continue
        for path in _walk_tasks(root):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(path)
    return sorted(found, key=lambda p: str(p))


def _walk_tasks(root: Path) -> Iterator[Path]:
    """Yield candidate task directories under ``root`` (depth ≤ 2).

    Skips ``.claude/worktrees`` and similar agent-scratch copies so the
    triad doesn't double-count tasks already covered by the canonical
    fixture path.
    """
    skip_segments = {".claude", "venv", ".venv", "build", "dist", ".tox"}

    def _looks_like_task(p: Path) -> bool:
        if not p.is_dir():
            return False
        has_meta = (p / "task.toml").is_file() or (p / "metadata.json").is_file()
        has_test = (p / "tests" / "test.sh").is_file()
        return has_meta and has_test

    if _looks_like_task(root):
        yield root
        return

    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if child.name in skip_segments:
            continue
        if _looks_like_task(child):
            yield child
            continue
        for grandchild in sorted(child.iterdir()):
            if not grandchild.is_dir():
                continue
            if grandchild.name in skip_segments:
                continue
            if _looks_like_task(grandchild):
                yield grandchild


def _detect_repo_root() -> Path:
    """Best-effort repo root detection for default task discovery.

    Walks up from the module file looking for a ``pyproject.toml``;
    falls back to the current working directory.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return Path.cwd()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
