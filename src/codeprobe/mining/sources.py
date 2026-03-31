"""Git host source detection — parse remote URLs to identify the hosting platform."""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

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
