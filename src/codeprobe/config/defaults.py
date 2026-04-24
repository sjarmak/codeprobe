"""Gate-on-context default resolvers (PRD §4 + §12.Q1-Q14 + M-Mod 5).

All new default behavior is GATED by env ``CODEPROBE_DEFAULTS``:

* ``v0.6`` (or unset, the default during the opt-in phase of M-Mod 5) —
  behavior is UNCHANGED. Callers get the classic Click-default wiring.
* ``v0.7`` — the resolvers in this module fire and fill in defaults
  based on repository shape / environment.

Each resolver is a PURE function with one deliberate exception:
:func:`resolve_narrative_source` delegates to ``core/llm.py`` (which
reads env vars for provider credentials) per PRD §13-T4. ``use_v07_defaults()``
is the other function that reads the environment. Resolvers return a
``(value, source)`` tuple where ``source`` is a short tag like
``'auto-detected'`` / ``'env'`` / ``'default'`` / ``'config-file'`` /
``'llm'`` / ``'offline-fallback'`` etc., so that callers can echo
provenance into their ``data.*_source`` envelope fields.

:func:`resolve_narrative_source` is ZFC-compliant: selection is
delegated to the model under a fixed rubric. When no LLM backend is
available (or ``offline=True``), the function falls back to the legacy
priority ``pr > commits > rfcs > issues`` and returns source
``'offline-fallback'`` so the caller can emit an ``LLM_UNAVAILABLE``
warning into the envelope.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------

CODEPROBE_DEFAULTS_ENV = "CODEPROBE_DEFAULTS"
_V07 = "v0.7"
_V06 = "v0.6"
_VALID_VALUES = frozenset({_V06, _V07, ""})


def use_v07_defaults() -> bool:
    """Return True when ``CODEPROBE_DEFAULTS=v0.7`` is set.

    Unset (or ``v0.6``) → False. Any other value is treated as ``v0.6``
    so that accidental typos never flip behavior.
    """
    return os.environ.get(CODEPROBE_DEFAULTS_ENV, "").strip() == _V07


# ---------------------------------------------------------------------------
# Prescriptive error (self-contained; folds into cli.errors when present)
# ---------------------------------------------------------------------------


class PrescriptiveError(Exception):
    """Raised by resolvers when context is insufficient to pick a default.

    Carries a short ``code`` (matches ``cli/error_codes.json``) and an
    actionable ``next_try_flag``/``next_try_value`` pair so callers can
    render a helpful error message without re-deriving the guidance.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        next_try_flag: str | None = None,
        next_try_value: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.next_try_flag = next_try_flag
        self.next_try_value = next_try_value

    def to_dict(self) -> dict[str, str | None]:
        return {
            "code": self.code,
            "message": self.message,
            "next_try_flag": self.next_try_flag,
            "next_try_value": self.next_try_value,
        }


class DiagnosticError(PrescriptiveError):
    """Alias kept for parity with ``cli/errors.py`` when it lands."""


# ---------------------------------------------------------------------------
# Repo shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepoShape:
    """Structural snapshot of a repository used by the resolvers.

    Populated by :func:`scan_repo_shape` via mechanical git probes. No
    semantic judgment lives in this dataclass — fields are pure facts.
    """

    repo_path: Path
    has_merged_prs: bool = False
    commit_count: int = 0
    has_rfcs: bool = False
    has_issues: bool = False
    has_mcp_config: bool = False
    pr_density: float = 0.0  # merged PRs / total commits; 0.0 when unknown
    size_loc: int = 0
    remote_url: str | None = None
    preamble_hints: tuple[str, ...] = field(default_factory=tuple)


def _git_run(
    args: list[str], cwd: Path, timeout: int = 10
) -> tuple[int, str]:
    """Run a git subcommand and return ``(returncode, stdout)``.

    Errors are swallowed to ``(1, "")`` because resolvers treat missing
    git output as "structural signal absent", which is a legitimate
    answer — not a failure we need to propagate.
    """
    try:
        res = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return res.returncode, res.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 1, ""


def scan_repo_shape(repo_path: Path) -> RepoShape:
    """Populate a :class:`RepoShape` from mechanical git probes.

    Pure IO. No model calls. All fields use structural signals only.
    """
    repo = Path(repo_path).resolve()

    # Commit count (0 for a fresh repo / non-repo).
    rc, out = _git_run(["rev-list", "--count", "HEAD"], repo)
    commit_count = int(out) if rc == 0 and out.isdigit() else 0

    # Merge commits (best-effort proxy for merged PRs — squash-only
    # repos will read as "no merged PRs" and the caller handles that
    # via fallback priorities). Using --merges is mechanical, not
    # semantic.
    rc, out = _git_run(
        ["log", "--merges", "--max-count=1", "--pretty=%H"], repo
    )
    has_merged_prs = rc == 0 and bool(out)

    # RFCs — structural file-glob, ZFC-compliant.
    rfc_dirs = [repo / "rfcs", repo / "RFCS", repo / "docs" / "rfcs"]
    has_rfcs = any(d.is_dir() for d in rfc_dirs)

    # Issues — no local signal, so we report False and let the caller
    # treat issues as unavailable.
    has_issues = False

    # MCP config — structural marker file check.
    mcp_markers = [
        repo / ".codeprobe" / "mcp.json",
        repo / "mcp.json",
        repo / ".mcp.json",
    ]
    has_mcp_config = any(p.is_file() for p in mcp_markers)

    # PR density proxy: merges / commits.
    rc, merges_out = _git_run(
        ["rev-list", "--merges", "--count", "HEAD"], repo
    )
    merge_count = int(merges_out) if rc == 0 and merges_out.isdigit() else 0
    pr_density = (merge_count / commit_count) if commit_count else 0.0

    # Rough size proxy: tracked file count (arbitrary but cheap).
    rc, files_out = _git_run(["ls-files"], repo)
    size_loc = len(files_out.splitlines()) if rc == 0 else 0

    # Remote for sg-repo derivation.
    rc, url_out = _git_run(["config", "--get", "remote.origin.url"], repo)
    remote_url = url_out if rc == 0 and url_out else None

    # Preamble hints (structural marker files).
    preamble_hints: list[str] = []
    if (repo / ".codeprobe" / "preamble.md").is_file():
        preamble_hints.append("custom")
    if (repo / ".github").is_dir():
        preamble_hints.append("github")
    if has_mcp_config:
        preamble_hints.append("sourcegraph")

    return RepoShape(
        repo_path=repo,
        has_merged_prs=has_merged_prs,
        commit_count=commit_count,
        has_rfcs=has_rfcs,
        has_issues=has_issues,
        has_mcp_config=has_mcp_config,
        pr_density=pr_density,
        size_loc=size_loc,
        remote_url=remote_url,
        preamble_hints=tuple(preamble_hints),
    )


# ---------------------------------------------------------------------------
# Resolvers — pure functions, each returns (value, source)
# ---------------------------------------------------------------------------


def resolve_goal(repo_shape: RepoShape) -> tuple[str, str]:
    """Pick the default ``--goal`` for a bare invocation.

    Priority (structural, Q1):

    1. ``mcp`` — when the repo has an MCP config file.
    2. ``quality`` — when PR density suggests merged-PR history exists.
    3. ``nav`` / ``general`` — for low-PR-density repos, fall back to
       ``general`` (balanced mix).

    Raises :class:`PrescriptiveError` with code ``GOAL_UNDETECTABLE``
    when the repo is effectively empty (no commits, no PRs, no RFCs,
    no MCP config).
    """
    if repo_shape.has_mcp_config:
        return "mcp", "auto-detected"
    if repo_shape.has_merged_prs and repo_shape.pr_density > 0.0:
        return "quality", "auto-detected"
    if repo_shape.commit_count > 0:
        return "general", "auto-detected"

    raise PrescriptiveError(
        code="GOAL_UNDETECTABLE",
        message=(
            "Cannot auto-detect --goal: repo has no commits, merged PRs, "
            "RFCs, or MCP config. Pass --goal explicitly."
        ),
        next_try_flag="--goal",
        next_try_value="quality",
    )


_GOAL_TO_TASK_TYPE = {
    "quality": "sdlc_code_change",
    "navigation": "architecture_comprehension",
    "nav": "architecture_comprehension",
    "mcp": "mcp_tool_usage",
    "general": "mixed",
}


def resolve_task_type(goal: str) -> tuple[str, str]:
    """Map a goal to the canonical task type."""
    task_type = _GOAL_TO_TASK_TYPE.get(goal.lower(), "mixed")
    return task_type, "auto-detected"


_NARRATIVE_CHOICES: tuple[str, ...] = ("pr", "commits", "rfcs", "issues")

# Fixed rubric used for the LLM-assisted narrative-source selection
# (PRD §13-T4). Kept as a module constant so tests can snapshot-diff it.
_NARRATIVE_RUBRIC_V1 = """\
You are selecting the narrative source that codeprobe should mine from a
git repository. The narrative source is the document type whose text best
explains WHY each change was made (needed for rich task instructions).

Pick EXACTLY ONE of: pr | commits | rfcs | issues.

Signal availability for this repo:
- has_merged_prs: {has_merged_prs}  (True ⇔ repo has merge commits; proxy for merged PR history)
- commit_count: {commit_count}
- has_rfcs: {has_rfcs}  (True ⇔ repo has rfcs/, RFCS/, or docs/rfcs/ directories)
- has_issues: {has_issues}
- pr_density: {pr_density}  (merge commits / total commits)

Rules:
- You MUST pick a source that is available (signal above is True / non-zero).
- Prefer "pr" when merged-PR narratives are likely rich (typical open-source repos).
- Prefer "commits" for squash-merge workflows, force-push workflows, or repos where
  commit messages are the most reliable narrative.
- Prefer "rfcs" when RFC docs are present and the repo has no merged-PR history.
- Prefer "issues" only as a last resort when the other three carry no narrative.

Respond with ONLY a single-line JSON object (no prose, no code fences):
{{"selected_source": "<one of: pr, commits, rfcs, issues>", "confidence": <0.0-1.0>, "source": "model"}}
"""

_JSON_OBJECT_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _deterministic_narrative_priority(
    repo_shape: RepoShape,
) -> tuple[str, ...]:
    """Legacy priority ``pr > commits > rfcs > issues`` used as offline fallback."""
    if repo_shape.has_merged_prs:
        return ("pr",)
    if repo_shape.commit_count > 0:
        return ("commits",)
    if repo_shape.has_rfcs:
        return ("rfcs",)
    if repo_shape.has_issues:
        return ("issues",)
    return ()


def _available_narrative_sources(repo_shape: RepoShape) -> set[str]:
    """Return the set of narrative sources with non-empty structural signal."""
    available: set[str] = set()
    if repo_shape.has_merged_prs:
        available.add("pr")
    if repo_shape.commit_count > 0:
        available.add("commits")
    if repo_shape.has_rfcs:
        available.add("rfcs")
    if repo_shape.has_issues:
        available.add("issues")
    return available


def _raise_undetectable() -> None:
    raise PrescriptiveError(
        code="NARRATIVE_SOURCE_UNDETECTABLE",
        message=(
            "Cannot auto-detect --narrative-source: repo has no merged PRs, "
            "commits, RFCs, or issues. Pass --narrative-source explicitly."
        ),
        next_try_flag="--narrative-source",
        next_try_value="commits",
    )


def _parse_narrative_model_response(
    text: str, available: set[str]
) -> str | None:
    """Extract and validate a narrative source from the model's raw text.

    Returns the selected source string on success, or ``None`` when the
    response is unparseable / invalid / picks an unavailable source.
    """
    if not text:
        return None
    match = _JSON_OBJECT_RE.search(text)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    selected = payload.get("selected_source")
    if not isinstance(selected, str):
        return None
    selected = selected.strip().lower()
    if selected not in _NARRATIVE_CHOICES:
        return None
    if selected not in available:
        return None
    return selected


def resolve_narrative_source(
    repo_shape: RepoShape,
    *,
    offline: bool = False,
) -> tuple[tuple[str, ...], str]:
    """Pick narrative source(s) via model judgment with deterministic fallback.

    Delegates selection to ``core/llm.py`` under the fixed rubric
    :data:`_NARRATIVE_RUBRIC_V1`. When ``offline=True`` or no LLM backend
    is available, falls back to the legacy priority
    ``pr > commits > rfcs > issues`` and returns source
    ``'offline-fallback'`` so the caller can emit an ``LLM_UNAVAILABLE``
    warning into the envelope.

    Source tags:

    - ``'llm'`` — LLM was consulted and its choice was accepted.
    - ``'offline-fallback'`` — offline mode requested or no LLM backend
      available; deterministic priority used. Caller SHOULD emit a
      warning with code ``LLM_UNAVAILABLE``.
    - ``'llm-unavailable'`` — an LLM backend was available but the call
      failed / returned garbage; deterministic priority used. Caller
      SHOULD emit a warning with code ``LLM_UNAVAILABLE``.

    Raises :class:`PrescriptiveError` ``NARRATIVE_SOURCE_UNDETECTABLE``
    when the repo has no PRs, commits, RFCs, or issues.
    """
    available = _available_narrative_sources(repo_shape)
    if not available:
        _raise_undetectable()
        return (), "default"  # unreachable; _raise_undetectable always raises

    # Local imports keep the defaults module dependency-light for the
    # (common) caller that never triggers the LLM path.
    from codeprobe.core.llm import (  # noqa: PLC0415
        LLMError,
        LLMRequest,
        call_llm,
        llm_available,
    )

    if offline or not llm_available():
        return _deterministic_narrative_priority(repo_shape), "offline-fallback"

    prompt = _NARRATIVE_RUBRIC_V1.format(
        has_merged_prs=repo_shape.has_merged_prs,
        commit_count=repo_shape.commit_count,
        has_rfcs=repo_shape.has_rfcs,
        has_issues=repo_shape.has_issues,
        pr_density=f"{repo_shape.pr_density:.3f}",
    )

    try:
        response = call_llm(
            LLMRequest(prompt=prompt, model="haiku", timeout_seconds=30)
        )
    except LLMError as exc:
        logger.debug("LLM narrative-source call failed: %s", exc)
        return _deterministic_narrative_priority(repo_shape), "llm-unavailable"

    selected = _parse_narrative_model_response(response.text, available)
    if selected is None:
        logger.debug(
            "LLM narrative-source response invalid or picked unavailable source: %r",
            response.text[:200],
        )
        return _deterministic_narrative_priority(repo_shape), "llm-unavailable"

    return (selected,), "llm"


def resolve_enrich(llm_available: bool) -> tuple[bool, str]:
    """Default ``--enrich`` to True only when an LLM is available."""
    if llm_available:
        return True, "llm-available"
    return False, "default"


def resolve_mcp_families(
    goal: str,
    env: Mapping[str, str],
    preamble: str | None,
) -> tuple[bool, str]:
    """Enable MCP families when all three signals agree (Q3).

    Signals:

    1. ``goal == 'mcp'``
    2. ``SOURCEGRAPH_TOKEN`` or ``SRC_ACCESS_TOKEN`` in env
    3. Sourcegraph preamble active OR ``--sg-repo`` derivation is possible
    """
    goal_ok = goal.lower() == "mcp"
    token_ok = bool(
        env.get("SOURCEGRAPH_TOKEN")
        or env.get("SRC_ACCESS_TOKEN")
        or env.get("SOURCEGRAPH_ACCESS_TOKEN")
    )
    preamble_ok = preamble == "sourcegraph" or preamble == "custom"

    if goal_ok and token_ok and preamble_ok:
        return True, "auto-detected"
    return False, "default"


_GITHUB_SSH_PREFIX = "git@github.com:"


def resolve_sg_repo(remote_url: str | None) -> tuple[str, str]:
    """Derive ``github.com/sg-evals/{repo_name}`` from the origin remote (Q3)."""
    if not remote_url:
        return "", "default"

    url = remote_url.strip()
    name = ""

    if url.startswith(_GITHUB_SSH_PREFIX):
        tail = url[len(_GITHUB_SSH_PREFIX):]
        # tail looks like "owner/repo.git"
        owner_repo = tail
    elif url.startswith(("http://", "https://")):
        # https://github.com/owner/repo.git or similar
        parts = url.split("/")
        if len(parts) >= 2:
            owner_repo = "/".join(parts[-2:])
        else:
            owner_repo = ""
    else:
        owner_repo = ""

    if owner_repo:
        stripped = owner_repo.rsplit(".git", 1)[0]
        _, _, name = stripped.rpartition("/")

    if not name:
        return "", "default"

    return f"github.com/sg-evals/{name}", "auto-detected"


def resolve_max_cost_usd() -> tuple[float, str]:
    """Default cumulative cost cap (Q6)."""
    return 10.00, "default"


def resolve_timeout(goal: str) -> tuple[int, str]:
    """Per-task timeout (Q7): 3600s for MCP, 600s otherwise."""
    if goal.lower() == "mcp":
        return 3600, "auto-detected"
    return 600, "default"


def resolve_experiment_config(cwd: Path) -> tuple[Path, str]:
    """Auto-discover a single experiment JSON under ``.codeprobe/``.

    Raises ``PrescriptiveError(AMBIGUOUS_EXPERIMENT)`` on zero or
    multiple candidates so the caller can render a prescriptive error.
    """
    root = Path(cwd).resolve()
    codeprobe_dir = root / ".codeprobe"

    candidates: list[Path] = []
    if (codeprobe_dir / "experiment.json").is_file():
        candidates.append(codeprobe_dir / "experiment.json")

    if codeprobe_dir.is_dir():
        for child in sorted(codeprobe_dir.iterdir()):
            if child.is_dir() and (child / "experiment.json").is_file():
                candidates.append(child / "experiment.json")

    if len(candidates) == 1:
        return candidates[0], "config-file"

    first = str(candidates[0]) if candidates else str(
        codeprobe_dir / "experiment.json"
    )
    raise PrescriptiveError(
        code="AMBIGUOUS_EXPERIMENT",
        message=(
            f"Found {len(candidates)} experiment.json files under "
            f"{codeprobe_dir}; pass --config to select one."
        ),
        next_try_flag="--config",
        next_try_value=first,
    )


def resolve_suite(cwd: Path) -> tuple[Path, str]:
    """Auto-discover a single ``suite.toml`` (Q10).

    Checks ``<cwd>/suite.toml`` then ``<cwd>/.codeprobe/suite.toml``.
    Raises ``PrescriptiveError(AMBIGUOUS_EXPERIMENT)`` on zero or
    multiple matches.
    """
    root = Path(cwd).resolve()
    candidates = [p for p in (root / "suite.toml", root / ".codeprobe" / "suite.toml") if p.is_file()]

    if len(candidates) == 1:
        return candidates[0], "config-file"

    first = str(candidates[0]) if candidates else str(root / "suite.toml")
    raise PrescriptiveError(
        code="AMBIGUOUS_EXPERIMENT",
        message=(
            f"Found {len(candidates)} suite.toml files under {root}; "
            "pass --suite to select one."
        ),
        next_try_flag="--suite",
        next_try_value=first,
    )


def resolve_out_calibrate(
    curator_version: str, now: date | None = None
) -> tuple[Path, str]:
    """Default output path for ``codeprobe calibrate`` (Q11).

    ``<cwd>/calibration_<curator-version>_<YYYYMMDD>.json``
    """
    when = now or date.today()
    stamp = when.strftime("%Y%m%d")
    filename = f"calibration_{curator_version}_{stamp}.json"
    return Path.cwd() / filename, "default"


_PREAMBLE_PRIORITY = ("custom", "github", "sourcegraph", "generic")


def resolve_preamble(repo_path: Path | None = None) -> tuple[str, str]:
    """Pick a preamble using priority custom > github > sourcegraph > generic (Q12)."""
    if repo_path is None:
        return "generic", "default"

    shape = scan_repo_shape(Path(repo_path))
    hints = set(shape.preamble_hints)
    for name in _PREAMBLE_PRIORITY:
        if name in hints:
            return name, "auto-detected"
    return "generic", "default"


# ---------------------------------------------------------------------------
# Compact doctor envelope helper
# ---------------------------------------------------------------------------


_COMPACT_BUDGET_BYTES = 2048


def compact_budget_bytes() -> int:
    """Return the M-Mod cross-cutting preamble size cap (2 KB)."""
    return _COMPACT_BUDGET_BYTES


__all__ = [
    "CODEPROBE_DEFAULTS_ENV",
    "PrescriptiveError",
    "DiagnosticError",
    "RepoShape",
    "compact_budget_bytes",
    "resolve_enrich",
    "resolve_experiment_config",
    "resolve_goal",
    "resolve_max_cost_usd",
    "resolve_mcp_families",
    "resolve_narrative_source",
    "resolve_out_calibrate",
    "resolve_preamble",
    "resolve_sg_repo",
    "resolve_suite",
    "resolve_task_type",
    "resolve_timeout",
    "scan_repo_shape",
    "use_v07_defaults",
]
