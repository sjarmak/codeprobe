#!/usr/bin/env python3
"""Release-artifact content gate.

Verified 2026-07-22: the published PyPI 0.11.0 sdist leaked 15 skill
directories (``.claude/skills/<name>/SKILL.md``, including deprecated
pre-v0.6.0 names like ``mine-tasks`` and ``run-eval``) via a
``recursive-include .claude/skills SKILL.md`` rule in ``MANIFEST.in`` that
swept whatever machine-local agent skills the build host happened to have.
The wheel was clean (skills are declared per-package in
``[tool.setuptools.package-data]``, not via a recursive glob) but nothing
checked the sdist, and nothing checked either artifact for version drift
against ``pyproject.toml`` or a matching ``CHANGELOG.md`` entry.

This script performs five deterministic, structural checks against a built
``dist/`` directory:

    (a) exactly one wheel and one sdist are present;
    (b) the wheel contains exactly the expected ``SKILL.md`` paths under
        ``codeprobe/skills_data/`` and nothing else;
    (c) the sdist contains exactly the expected ``SKILL.md`` paths under
        ``src/codeprobe/skills_data/`` and nothing else (this is the check
        that would have caught the 0.11.0 leak);
    (d) the wheel and sdist filenames encode the same version as
        ``pyproject.toml``;
    (e) ``CHANGELOG.md`` has a ``## <version>`` heading for that version.

Every check is a pure string/archive-listing comparison — no judgment calls,
no heuristics. All checks run and are reported together rather than
stopping at the first failure, so a single invocation surfaces everything
wrong with a release candidate.

Usage:
    python scripts/check_release_artifacts.py dist/
    python scripts/check_release_artifacts.py dist/ --version 0.12.0
    python scripts/check_release_artifacts.py dist/ --repo /path/to/repo

Exit codes:
    0   all checks pass
    1   at least one violation (one per line, on stderr)
    2   CLI / IO error (dist dir missing, pyproject.toml unreadable, etc.)

Stdlib only — zipfile/tarfile/tomllib ship with Python 3.11+; this script
must run before the project's build/dev dependencies are installed (it is
what decides whether those dependencies get published).
"""

from __future__ import annotations

import argparse
import re
import sys
import tarfile
import tomllib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Structural configuration
# ---------------------------------------------------------------------------

#: The skills codeprobe actually ships, per
#: ``[tool.setuptools.package-data] "codeprobe.skills_data" = ["*/SKILL.md"]``.
EXPECTED_SKILL_NAMES: tuple[str, ...] = (
    "codeprobe-calibrate",
    "codeprobe-check-infra",
    "codeprobe-interpret",
    "codeprobe-mine",
    "codeprobe-run",
)

#: Wheel layout: installed package paths, no "src/" prefix.
EXPECTED_WHEEL_SKILL_PATHS: frozenset[str] = frozenset(
    f"codeprobe/skills_data/{name}/SKILL.md" for name in EXPECTED_SKILL_NAMES
)

#: Sdist layout mirrors the source tree under "src/", after stripping the
#: "<project>-<version>/" top-level directory every sdist tarball wraps its
#: contents in (see ``sdist_skill_paths``).
EXPECTED_SDIST_SKILL_PATHS: frozenset[str] = frozenset(
    f"src/codeprobe/skills_data/{name}/SKILL.md" for name in EXPECTED_SKILL_NAMES
)

#: The 15 skill paths that leaked into the published 0.11.0 sdist. Checked
#: explicitly, on top of the generic "no unexpected SKILL.md" rule below, so
#: a regression is diagnosed by name instead of a generic "unexpected path".
LEAKED_0_11_0_SKILL_PATHS: frozenset[str] = frozenset(
    f".claude/skills/{name}/SKILL.md"
    for name in (
        "acceptance-loop",
        "assess-codebase",
        "codeprobe-calibrate",
        "codeprobe-check-infra",
        "codeprobe-interpret",
        "codeprobe-mine",
        "codeprobe-run",
        "experiment",
        "integration-test",
        "interpret",
        "mine-tasks",
        "probe",
        "ratings",
        "run-eval",
        "scaffold",
    )
)

#: Matches the version segment of a wheel or sdist filename, e.g.
#: "codeprobe-0.12.0-py3-none-any.whl" -> "0.12.0", "codeprobe-0.12.0.tar.gz"
#: -> "0.12.0".
_ARTIFACT_VERSION_RE = re.compile(
    r"^codeprobe-([0-9][0-9a-zA-Z.]*?)(?:-py3-none-any\.whl|\.tar\.gz)$"
)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ArtifactCheckResult:
    """Aggregated outcome of every check. ``ok`` iff there are no violations."""

    violations: list[str] = field(default_factory=list)
    wheel_path: Path | None = None
    sdist_path: Path | None = None

    @property
    def ok(self) -> bool:
        return not self.violations


# ---------------------------------------------------------------------------
# Individual checks (importable, independently testable)
# ---------------------------------------------------------------------------


def find_artifacts(dist_dir: Path) -> tuple[list[str], Path | None, Path | None]:
    """(a) Exactly one wheel and one sdist under ``dist_dir``."""
    violations: list[str] = []
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if len(wheels) != 1:
        violations.append(
            f"expected exactly one wheel under {dist_dir}, found {len(wheels)}: "
            f"{[w.name for w in wheels]}"
        )
    if len(sdists) != 1:
        violations.append(
            f"expected exactly one sdist under {dist_dir}, found {len(sdists)}: "
            f"{[s.name for s in sdists]}"
        )
    wheel = wheels[0] if len(wheels) == 1 else None
    sdist = sdists[0] if len(sdists) == 1 else None
    return violations, wheel, sdist


def wheel_skill_paths(wheel_path: Path) -> list[str]:
    """``SKILL.md`` entries in a wheel, exactly as they appear in the archive."""
    with zipfile.ZipFile(wheel_path) as zf:
        return [n for n in zf.namelist() if n.endswith("SKILL.md")]


def sdist_skill_paths(sdist_path: Path) -> list[str]:
    """``SKILL.md`` entries in an sdist, with the mandatory
    "<project>-<version>/" top-level directory stripped off."""
    with tarfile.open(sdist_path) as tf:
        names = tf.getnames()
    stripped = []
    for name in names:
        if not name.endswith("SKILL.md"):
            continue
        _, _, rest = name.partition("/")
        stripped.append(rest or name)
    return stripped


def check_skill_paths(
    actual: list[str], expected: frozenset[str], archive_label: str
) -> list[str]:
    """(b)/(c): exactly the expected ``SKILL.md`` paths, nothing else."""
    violations: list[str] = []
    found = set(actual)
    missing = expected - found
    if missing:
        violations.append(
            f"{archive_label}: missing expected skill path(s): {sorted(missing)}"
        )
    unexpected = sorted(found - expected)
    if unexpected:
        violations.append(
            f"{archive_label}: unexpected SKILL.md path(s) present (only the "
            f"{len(expected)} packaged skills may ship): {unexpected}"
        )
    leaked = sorted(found & LEAKED_0_11_0_SKILL_PATHS)
    if leaked:
        violations.append(
            f"{archive_label}: denylisted path(s) from the 0.11.0 MANIFEST.in "
            f"leak are present again: {leaked}"
        )
    return violations


def artifact_version(path: Path) -> str | None:
    """(d) helper: parse the version out of a wheel/sdist filename."""
    match = _ARTIFACT_VERSION_RE.match(path.name)
    return match.group(1) if match else None


def read_pyproject_version(pyproject_path: Path) -> str:
    with pyproject_path.open("rb") as fh:
        data = tomllib.load(fh)
    version = (data.get("project") or {}).get("version")
    if not isinstance(version, str) or not version:
        raise ValueError(f"{pyproject_path} has no [project].version string")
    return version


def check_versions_match(
    wheel_path: Path, sdist_path: Path, expected_version: str
) -> list[str]:
    """(d) wheel and sdist filenames encode ``expected_version``."""
    violations: list[str] = []
    wheel_version = artifact_version(wheel_path)
    if wheel_version != expected_version:
        violations.append(
            f"wheel filename version {wheel_version!r} does not match "
            f"pyproject.toml version {expected_version!r} ({wheel_path.name})"
        )
    sdist_version = artifact_version(sdist_path)
    if sdist_version != expected_version:
        violations.append(
            f"sdist filename version {sdist_version!r} does not match "
            f"pyproject.toml version {expected_version!r} ({sdist_path.name})"
        )
    return violations


def check_changelog_has_heading(changelog_path: Path, version: str) -> list[str]:
    """(e) ``CHANGELOG.md`` has a ``## <version>`` heading."""
    if not changelog_path.is_file():
        return [f"{changelog_path} does not exist"]
    text = changelog_path.read_text(encoding="utf-8")
    # (?=\s|$) rather than \b: \b alone would let a dotted-prefix version
    # ("0.11" against a real "0.11.0" heading) match, since the digit->dot
    # transition also satisfies a bare word boundary.
    heading_re = re.compile(rf"^##\s+{re.escape(version)}(?=\s|$)", re.MULTILINE)
    if not heading_re.search(text):
        return [f"{changelog_path} has no '## {version}' heading"]
    return []


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def check_release_artifacts(
    dist_dir: Path, repo_root: Path, tag_version: str | None = None
) -> ArtifactCheckResult:
    """Run all five checks and return the aggregated result.

    ``pyproject.toml``'s ``[project].version`` is the source of truth for
    check (d)/(e). If ``tag_version`` (e.g. derived from the pushed git tag
    in CI) is also given, it must match the pyproject version too — this
    catches a release tagged without the corresponding version bump.
    """
    pyproject_version = read_pyproject_version(repo_root / "pyproject.toml")

    violations, wheel, sdist = find_artifacts(dist_dir)
    result = ArtifactCheckResult(
        violations=violations, wheel_path=wheel, sdist_path=sdist
    )
    if wheel is not None:
        result.violations += check_skill_paths(
            wheel_skill_paths(wheel), EXPECTED_WHEEL_SKILL_PATHS, "wheel"
        )
    if sdist is not None:
        result.violations += check_skill_paths(
            sdist_skill_paths(sdist), EXPECTED_SDIST_SKILL_PATHS, "sdist"
        )
    if wheel is not None and sdist is not None:
        result.violations += check_versions_match(wheel, sdist, pyproject_version)
    result.violations += check_changelog_has_heading(
        repo_root / "CHANGELOG.md", pyproject_version
    )
    if tag_version is not None and tag_version != pyproject_version:
        result.violations.append(
            f"--version {tag_version!r} (from the pushed tag) does not match "
            f"pyproject.toml version {pyproject_version!r}"
        )
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _find_repo_root(start: Path) -> Path | None:
    """Walk upward from ``start`` to find a directory containing ``.git``."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assert content, version, and changelog invariants on "
        "a built dist/ directory before it is published."
    )
    parser.add_argument(
        "dist_dir", type=Path, help="Directory containing the built wheel + sdist."
    )
    parser.add_argument(
        "--version",
        default=None,
        help="Release version to cross-check against pyproject.toml (e.g. "
        "derived from the pushed git tag). Optional.",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="Repo root (default: walk upward from dist_dir).",
    )
    args = parser.parse_args(argv)

    dist_dir: Path = args.dist_dir
    if not dist_dir.is_dir():
        print(f"error: {dist_dir} is not a directory", file=sys.stderr)
        return 2

    repo_root = args.repo or _find_repo_root(dist_dir) or _find_repo_root(Path.cwd())
    if repo_root is None:
        print(
            "error: could not locate a git repository (use --repo to override)",
            file=sys.stderr,
        )
        return 2

    try:
        result = check_release_artifacts(dist_dir, repo_root, args.version)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if result.ok:
        wheel_name = result.wheel_path.name if result.wheel_path else "?"
        sdist_name = result.sdist_path.name if result.sdist_path else "?"
        print(f"OK: {wheel_name}, {sdist_name} pass all release-artifact checks")
        return 0

    print("RELEASE ARTIFACT CHECK FAILED:", file=sys.stderr)
    for violation in result.violations:
        print(f"  - {violation}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
