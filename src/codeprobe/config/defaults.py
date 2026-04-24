"""Gate-on-context default resolvers (PRD §4 + §12.Q1-Q14 + M-Mod 5).

All new default behavior is GATED by env ``CODEPROBE_DEFAULTS``:

* ``v0.6`` (or unset, the default during the opt-in phase of M-Mod 5) —
  behavior is UNCHANGED. Callers get the classic Click-default wiring.
* ``v0.7`` — the resolvers in this module fire and fill in defaults
  based on repository shape / environment.

Each resolver is a PURE function. ``use_v07_defaults()`` is the only
function in this module that reads the environment. Resolvers return a
``(value, source)`` tuple where ``source`` is a short tag like
``'auto-detected'`` / ``'env'`` / ``'default'`` / ``'config-file'`` /
``'llm-available'`` etc., so that callers can echo provenance into
their ``data.*_source`` envelope fields.

ZFC note: :func:`resolve_narrative_source` uses a mechanical priority
rule (``pr > commits > rfcs > issues``). That priority is a *semantic
tiebreaker* — not a structural check — so it is tracked as ZFC debt in
``CLAUDE.md § Known violations`` with a dated SLO enforced by
``tests/zfc/test_narrative_source_slo.py``.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path


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


def resolve_narrative_source(
    repo_shape: RepoShape,
) -> tuple[tuple[str, ...], str]:
    """Pick narrative source(s) using the priority ``pr > commits > rfcs > issues``.

    This is a SEMANTIC TIEBREAKER and is therefore tracked in
    ``CLAUDE.md § Known violations``. It is self-enforced by
    ``tests/zfc/test_narrative_source_slo.py`` with deadline
    ``2026-10-23``.

    Raises :class:`PrescriptiveError` ``NARRATIVE_SOURCE_UNDETECTABLE``
    when the repo has no PRs, commits, RFCs, or issues.
    """
    if repo_shape.has_merged_prs:
        return ("pr",), "auto-detected"
    if repo_shape.commit_count > 0:
        return ("commits",), "auto-detected"
    if repo_shape.has_rfcs:
        return ("rfcs",), "auto-detected"
    if repo_shape.has_issues:
        return ("issues",), "auto-detected"

    raise PrescriptiveError(
        code="NARRATIVE_SOURCE_UNDETECTABLE",
        message=(
            "Cannot auto-detect --narrative-source: repo has no merged PRs, "
            "commits, RFCs, or issues. Pass --narrative-source explicitly."
        ),
        next_try_flag="--narrative-source",
        next_try_value="commits",
    )


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
