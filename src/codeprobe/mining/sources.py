"""Git host source detection + narrative adapter protocol.

Two concerns live in this module:

* ``RepoSource`` / ``detect_source`` — parse remote URLs to identify the
  hosting platform (GitHub, GitLab, Azure DevOps, self-hosted, local).
* ``NarrativeAdapter`` / ``NarrativeBundle`` — pluggable interface for
  fetching narrative context (PR bodies, commit messages, RFC docs) for a
  given commit. Concrete adapters live in :mod:`codeprobe.mining.adapters`.

The narrative protocol enforces INV1 ("no silent fallback"): callers must
explicitly select which narrative source(s) to use when the default PR
path is unavailable. See :func:`select_narrative_adapters`.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

_HOST_MAP: dict[str, str] = {
    "github.com": "github",
    "gitlab.com": "gitlab",
    "bitbucket.org": "bitbucket",
    "dev.azure.com": "azure",
}

# Matches HTTPS URLs like https://github.com/owner/repo.git
_HTTPS_PATTERN = re.compile(
    r"https?://(?P<host>[^/]+)/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$"
)

# Matches SSH URLs like git@github.com:owner/repo.git
_SSH_PATTERN = re.compile(
    r"git@(?P<host>[^:]+):(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$"
)

# Matches Azure DevOps HTTPS: https://dev.azure.com/org/project/_git/repo
_AZURE_HTTPS_PATTERN = re.compile(
    r"https?://dev\.azure\.com/(?P<owner>[^/]+)/[^/]+/_git/(?P<repo>[^/]+?)(?:\.git)?$"
)


@dataclass(frozen=True)
class RepoSource:
    """Identified git hosting source for a repository."""

    host: str
    owner: str
    repo: str
    remote_url: str


def _classify_host(hostname: str) -> str:
    """Map a hostname to a known host key, or 'self-hosted' for unknown hosts."""
    return _HOST_MAP.get(hostname, "self-hosted")


def _parse_remote_url(url: str) -> RepoSource:
    """Parse a git remote URL into a RepoSource."""
    # Try Azure DevOps first (more specific pattern)
    match = _AZURE_HTTPS_PATTERN.match(url)
    if match:
        return RepoSource(
            host="azure",
            owner=match.group("owner"),
            repo=match.group("repo"),
            remote_url=url,
        )

    # Try HTTPS
    match = _HTTPS_PATTERN.match(url)
    if match:
        return RepoSource(
            host=_classify_host(match.group("host")),
            owner=match.group("owner"),
            repo=match.group("repo"),
            remote_url=url,
        )

    # Try SSH
    match = _SSH_PATTERN.match(url)
    if match:
        return RepoSource(
            host=_classify_host(match.group("host")),
            owner=match.group("owner"),
            repo=match.group("repo"),
            remote_url=url,
        )

    logger.warning("Could not parse remote URL: %s", url)
    return RepoSource(host="local", owner="", repo="", remote_url=url)


def detect_source(path: Path) -> RepoSource:
    """Detect the git hosting source for a repository at *path*.

    Runs ``git remote get-url origin`` and parses the result.
    Returns a local source when no remote is configured.
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.info("No git remote origin found at %s — treating as local", path)
            return RepoSource(
                host="local",
                owner="",
                repo=path.name,
                remote_url="",
            )
        url = result.stdout.strip()
        return _parse_remote_url(url)
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Failed to detect git remote: %s", exc)
        return RepoSource(
            host="local",
            owner="",
            repo=path.name,
            remote_url="",
        )


# ---------------------------------------------------------------------------
# Narrative adapter protocol (r9 — pluggable enrichment sources)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NarrativeBundle:
    """Raw narrative context for a commit, produced by a :class:`NarrativeAdapter`.

    The task mining pipeline consumes ``text`` as the PR-body-equivalent
    input to LLM instruction generation. ``metadata`` is an adapter-specific
    key/value map that the writer may surface (e.g. ``{"pr_number": "42"}``)
    but that is NOT interpreted by the pipeline — adapters embed any
    structural context they have.

    ``source_name`` mirrors the adapter's ``name`` attribute and is the
    value that gets written into ``TaskMetadata.enrichment_source``.
    """

    text: str
    metadata: dict[str, str] = field(default_factory=dict)
    source_name: str = ""


@runtime_checkable
class NarrativeAdapter(Protocol):
    """Protocol for a pluggable narrative source.

    Implementations live in :mod:`codeprobe.mining.adapters`. Each adapter
    fetches a :class:`NarrativeBundle` describing ``commit_sha`` in ``repo``
    and returns ``None`` when it has no narrative to offer. Returning
    ``None`` lets the caller fall through to the next adapter in the
    user-selected chain; it is NOT a silent-fallback escape hatch — the
    CLI enforces that *some* adapter was explicitly selected.
    """

    name: str

    def fetch(self, repo: Path, commit_sha: str) -> NarrativeBundle | None:
        """Fetch narrative for ``commit_sha`` in ``repo``.

        Returns ``None`` when the adapter has nothing to contribute.
        Must not raise on missing inputs (e.g. ``gh`` not installed, no
        RFC directory) — just return ``None`` so the next adapter runs.
        """
        ...


# Canonical adapter name registry. Populated lazily in
# :func:`select_narrative_adapters` to keep import order safe.
_KNOWN_ADAPTER_NAMES: frozenset[str] = frozenset({"pr", "commits", "rfcs"})


def parse_narrative_selection(raw: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Normalize a click ``multiple=True`` tuple into a deduped adapter-name tuple.

    Each entry in *raw* may itself be comma- or plus-separated — so
    ``("commits+rfcs",)`` and ``("commits", "rfcs")`` both yield
    ``("commits", "rfcs")``. Order is preserved on first occurrence.
    Whitespace and case are normalized.
    """
    seen: dict[str, None] = {}
    for entry in raw:
        for part in re.split(r"[,+]", entry):
            name = part.strip().lower()
            if name and name not in seen:
                seen[name] = None
    return tuple(seen.keys())


def select_narrative_adapters(selection: tuple[str, ...]) -> list[NarrativeAdapter]:
    """Resolve adapter names to concrete adapter instances.

    Unknown names raise :class:`ValueError` with a message naming the
    accepted set — this is a hard failure, not a silent skip (INV1).
    """
    # Deferred import breaks a cycle: adapters import from this module for
    # the Protocol, so we can only import them here at call time.
    from codeprobe.mining.adapters import build_adapter

    if not selection:
        return []

    adapters: list[NarrativeAdapter] = []
    for name in selection:
        if name not in _KNOWN_ADAPTER_NAMES:
            raise ValueError(
                f"Unknown narrative source {name!r}. "
                f"Accepted values: {sorted(_KNOWN_ADAPTER_NAMES)}. "
                "Pass e.g. --narrative-source commits+rfcs."
            )
        adapters.append(build_adapter(name))
    return adapters


def has_pr_narratives(path: Path, timeout: int = 10) -> bool:
    """Return True when the repo has at least one merged PR discoverable via ``gh``.

    Used by the CLI to decide whether to default to the ``pr`` adapter or
    raise a loud error requesting explicit ``--narrative-source``. A
    missing or failing ``gh`` CLI counts as "no PRs available" — callers
    must choose a non-PR adapter explicitly in that case.
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "merged",
                "--limit",
                "1",
                "--json",
                "number",
            ],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    if result.returncode != 0:
        return False
    stdout = result.stdout.strip()
    # Empty array means no merged PRs; any non-empty JSON list means yes.
    return bool(stdout) and stdout not in ("[]", "null")
