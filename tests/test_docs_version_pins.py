"""Docs that pin a concrete image tag must track the package version.

The container image tag defaults to the installed codeprobe version
(``DEFAULT_IMAGE_VERSION`` / ``_installed_version()`` in
``sandbox/runner.py``), so a documented ``codeprobe-agent:0.13.0`` stops
being a command anyone can paste the moment the package is bumped. Every
release cut before this guard left these pins behind — the 0.14.0 bump
found them still on ``0.14.0rc2``.

Version-like strings in prose (changelog history, "since 0.9.0", the
release runbook's ``vX.Y.Z`` placeholder) are deliberately out of scope;
this only matches tags and env assignments a reader would copy.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATHS = sorted((REPO_ROOT / "docs").rglob("*.md"))

# `codeprobe-agent:0.14.0`, `codeprobe-scoring:0.14.0`, and
# `CODEPROBE_IMAGE_VERSION=0.14.0` — the three shapes that resolve to a
# real image tag when pasted.
_PINNED_TAG = re.compile(
    r"(?:codeprobe-(?:agent|scoring):|CODEPROBE_IMAGE_VERSION=)"
    r"(\d+\.\d+\.\d+(?:rc\d+)?)"
)


@pytest.fixture(scope="module")
def package_version() -> str:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    assert isinstance(version, str)
    return version


def test_docs_exist() -> None:
    """Guard the guard: an empty glob would make every assertion vacuous."""
    assert DOC_PATHS, "no docs found under docs/ — has the tree moved?"


def test_documented_image_tags_match_package_version(package_version: str) -> None:
    stale: list[str] = []
    for doc in DOC_PATHS:
        for lineno, line in enumerate(
            doc.read_text(encoding="utf-8").splitlines(), 1
        ):
            for found in _PINNED_TAG.findall(line):
                if found != package_version:
                    rel = doc.relative_to(REPO_ROOT)
                    stale.append(f"{rel}:{lineno} pins {found}")
    assert not stale, (
        f"docs pin an image tag other than the package version "
        f"{package_version!r}; the tag defaults to the installed version, so "
        f"these commands would fail as written: {stale}"
    )
