"""Dependency-upgrade task generator.

Mines merged PRs whose diff touches only dependency manifest / lockfile files
(e.g. ``package.json`` + ``pnpm-lock.yaml``) and whose title contains a
version-like token (``bump X to 1.2.3``). Each surviving candidate is routed
through :func:`codeprobe.core.llm.call_claude` for a task-type classification
judgment before a :class:`~codeprobe.models.task.Task` is emitted — this is
the ZFC-compliant path: the semantic judgment (is this really a dependency
upgrade?) is delegated to the model; the generator code does only IO,
structural filtering, and mechanical assembly.

ZFC compliance:

* The structural filter (:func:`is_dependency_upgrade_candidate`) is pure
  set/regex checking — allowed under ZFC as mechanical preprocessing.
* The task-type acceptance decision goes through ``call_claude``. When the
  LLM is unavailable or errors, :func:`classify_with_model` returns the
  conservative ``("reject", "llm unavailable")`` so we don't silently
  promote a candidate using a hardcoded heuristic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from codeprobe.core.llm import (
    LLMError,
    LLMRequest,
    call_claude,
    llm_available,
)
from codeprobe.models.task import Task, TaskMetadata, TaskVerification

logger = logging.getLogger(__name__)

__all__ = [
    "DEPENDENCY_MANIFESTS",
    "PRCandidate",
    "is_dependency_upgrade_candidate",
    "classify_with_model",
    "generate_tasks",
]


# ---------------------------------------------------------------------------
# Structural signals — not heuristics per ZFC (mechanical file-set membership
# + regex tokenization are allowed as boundary preprocessing).
# ---------------------------------------------------------------------------

#: File basenames that identify a dependency manifest or lockfile. Basename
#: matching (vs full-path matching) lets the filter work in monorepos where
#: manifests live in sub-directories (``packages/foo/package.json``).
DEPENDENCY_MANIFESTS: frozenset[str] = frozenset(
    {
        "package.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "package-lock.json",
        "go.sum",
        "go.mod",
        "Cargo.toml",
        "Cargo.lock",
        "requirements.txt",
        "pyproject.toml",
        "poetry.lock",
        "Pipfile",
        "Pipfile.lock",
        "Gemfile",
        "Gemfile.lock",
    }
)

#: Matches a version-like semver token anywhere in the PR title.
_SEMVER_RE: re.Pattern[str] = re.compile(r"\b\d+\.\d+(?:\.\d+)?\b")

#: Matches the "bump X to Y" idiom used by Dependabot / Renovate.
_BUMP_RE: re.Pattern[str] = re.compile(r"\bbump\b.*\bto\b", re.IGNORECASE)

#: Cap on the number of changed files we'll even consider — a diff bigger
#: than this is almost certainly not a pure dependency bump.
_MAX_CHANGED_FILES: int = 40

#: Timeout for the classifier LLM call.
_CLASSIFIER_TIMEOUT_SECONDS: int = 20

#: Timeout for git diff used when resolving changed files from a sha.
_GIT_DIFF_TIMEOUT: int = 15


@dataclass(frozen=True)
class PRCandidate:
    """Minimal shape a PR must present to the generator.

    Kept narrow so call-sites can construct it from either a ``MergedPR``
    + ``_get_changed_files`` in :mod:`codeprobe.mining.extractor` or from
    a test fixture without pulling in a live repo.
    """

    sha: str
    title: str
    body: str
    changed_files: tuple[str, ...]


# ---------------------------------------------------------------------------
# Structural filter
# ---------------------------------------------------------------------------


def is_dependency_upgrade_candidate(pr: PRCandidate) -> bool:
    """Return True when *pr* structurally looks like a dependency upgrade.

    Two conditions:

    1. Every changed file's basename is in :data:`DEPENDENCY_MANIFESTS`.
       (An empty changed_files set is rejected — nothing to mine.)
    2. The PR title contains a semver-like token OR the ``bump ... to ...``
       idiom.

    This is a pure structural check (set membership + regex) and does not
    count as a ZFC judgment — no semantic classification happens here.
    """
    if not pr.changed_files:
        return False
    if len(pr.changed_files) > _MAX_CHANGED_FILES:
        return False
    for path in pr.changed_files:
        basename = Path(path).name
        if basename not in DEPENDENCY_MANIFESTS:
            return False
    title = pr.title or ""
    return bool(_SEMVER_RE.search(title) or _BUMP_RE.search(title))


# ---------------------------------------------------------------------------
# Model-delegated classifier — ZFC path.
# ---------------------------------------------------------------------------


def _build_classification_prompt(pr: PRCandidate) -> str:
    """Compose the Haiku prompt for dependency-upgrade classification."""
    files_preview = ", ".join(list(pr.changed_files)[:20]) or "(no files)"
    body_preview = (pr.body or "").strip()
    if len(body_preview) > 600:
        body_preview = body_preview[:600] + "... [truncated]"
    return (
        "You are classifying merged pull requests by task type.\n\n"
        "The candidate PR has already passed a structural filter: its diff\n"
        "touches only dependency manifest / lockfile files and its title\n"
        "contains a version-like token. Your job is to confirm that this is\n"
        "really a dependency upgrade (vs. an initial manifest commit, a\n"
        "license change, or a format cleanup).\n\n"
        f"**PR title:** {pr.title}\n"
        f"**Changed files:** {files_preview}\n"
        f"**PR body:** {body_preview}\n\n"
        "Return a JSON object exactly of the form:\n"
        '{"decision": "accept"|"reject", "rationale": "<one short sentence>"}\n'
        "No markdown, no code fences, no commentary outside the JSON."
    )


def _parse_classification_response(text: str) -> tuple[str, str]:
    """Validate-or-die parser for the classifier response.

    Returns ``("accept"|"reject", rationale)``. Returns ``("reject", <reason>)``
    on any parse failure or malformed shape rather than defaulting to accept
    — a malformed LLM response must never silently promote a task.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        cleaned = "\n".join(
            line for line in lines if not line.strip().startswith("```")
        )
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return ("reject", "malformed classifier response")
    if not isinstance(parsed, dict):
        return ("reject", "classifier response not an object")
    decision = parsed.get("decision", "")
    rationale = parsed.get("rationale", "")
    if not isinstance(decision, str) or decision not in ("accept", "reject"):
        return ("reject", "invalid decision value")
    if not isinstance(rationale, str):
        rationale = ""
    return (decision, rationale)


def classify_with_model(pr: PRCandidate) -> tuple[str, str]:
    """Run the PR through the model classifier.

    Returns ``(decision, rationale)`` where ``decision`` is ``"accept"`` or
    ``"reject"``. When the LLM is unavailable or errors, returns
    ``("reject", "llm unavailable")`` so the generator fails closed — a
    hardcoded accept on LLM failure would defeat the ZFC delegation.
    """
    if not llm_available():
        return ("reject", "llm unavailable")
    prompt = _build_classification_prompt(pr)
    try:
        response = call_claude(
            LLMRequest(
                prompt=prompt,
                model="haiku",
                timeout_seconds=_CLASSIFIER_TIMEOUT_SECONDS,
            )
        )
    except LLMError as exc:
        logger.warning("dependency_upgrade classifier LLM error: %s", exc)
        return ("reject", f"llm error: {exc}")
    return _parse_classification_response(response.text)


# ---------------------------------------------------------------------------
# Task assembly — called AFTER classify_with_model, so the ZFC lint sees a
# ``call_claude`` invocation in the enclosing function scope before any
# ``TaskMetadata(...)`` construction.
# ---------------------------------------------------------------------------


def _task_id(repo_name: str, pr: PRCandidate) -> str:
    seed = f"depup-{repo_name}-{pr.sha}-{pr.title}"
    return hashlib.sha256(seed.encode()).hexdigest()[:10]


def _language_for_manifests(changed_files: Iterable[str]) -> str:
    """Infer a coarse language label from the manifest basenames.

    Deterministic map — this is not a semantic judgment, just a
    lookup-by-file-extension equivalent.
    """
    basenames = {Path(f).name for f in changed_files}
    if basenames & {"package.json", "pnpm-lock.yaml", "yarn.lock",
                    "package-lock.json"}:
        return "javascript"
    if basenames & {"go.mod", "go.sum"}:
        return "go"
    if basenames & {"Cargo.toml", "Cargo.lock"}:
        return "rust"
    if basenames & {"pyproject.toml", "requirements.txt", "poetry.lock",
                    "Pipfile", "Pipfile.lock"}:
        return "python"
    if basenames & {"Gemfile", "Gemfile.lock"}:
        return "ruby"
    return ""


def _get_pr_changed_files(repo: Path, sha: str) -> tuple[str, ...]:
    """Resolve changed files for a PR sha via ``git diff``. Empty on failure."""
    try:
        result = subprocess.run(
            ["git", "diff", f"{sha}^..{sha}", "--name-only"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=_GIT_DIFF_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("git diff failed for %s: %s", sha, exc)
        return ()
    if result.returncode != 0:
        return ()
    return tuple(
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip()
    )


def generate_tasks(
    repo: Path,
    prs: Iterable[PRCandidate],
    *,
    no_llm: bool = False,
) -> list[Task]:
    """Return dependency-upgrade Tasks mined from *prs*.

    The flow for each PR is:

    1. Structural filter (:func:`is_dependency_upgrade_candidate`).
    2. Model classifier (:func:`classify_with_model`) — called via
       ``call_claude`` so the ZFC lint sees a model invocation before any
       ``TaskMetadata`` assembly in this function.
    3. If accepted, build a :class:`Task` with
       ``metadata.task_type == "dependency_upgrade"``.

    When ``no_llm=True`` the model step is skipped and candidates that
    survive the structural filter are accepted directly. This path is
    intended for offline/deterministic tests only — production mining
    always runs the classifier.
    """
    repo_name = repo.name or "repo"
    tasks: list[Task] = []

    for pr in prs:
        if not is_dependency_upgrade_candidate(pr):
            continue

        if no_llm:
            decision, rationale = ("accept", "no_llm mode")
        else:
            # IMPORTANT: this call MUST happen before any TaskMetadata(...)
            # construction in this function so the ZFC lint registers a
            # model invocation in the enclosing scope.
            decision, rationale = classify_with_model(pr)

        if decision != "accept":
            logger.debug(
                "dependency_upgrade: rejected %s — %s",
                pr.sha[:8] if pr.sha else "(no sha)",
                rationale,
            )
            continue

        language = _language_for_manifests(pr.changed_files)
        task_id = _task_id(repo_name, pr)
        description_preview = pr.title or "dependency upgrade"

        metadata = TaskMetadata(
            name=f"depup-{task_id}",
            description=description_preview,
            language=language,
            category="sdlc",
            task_type="dependency_upgrade",
            issue_title=pr.title or f"Dependency upgrade {task_id}",
            issue_body=pr.body or pr.title or "",
            ground_truth_commit=pr.sha,
            enrichment_source="llm" if not no_llm else "",
            tool_benefit_rationale=rationale,
        )
        verification = TaskVerification(
            type="test_script",
            command="bash tests/test.sh",
            verification_mode="test_script",
            reward_type="binary",
        )
        tasks.append(
            Task(
                id=task_id,
                repo=repo_name,
                metadata=metadata,
                verification=verification,
            )
        )

    return tasks
