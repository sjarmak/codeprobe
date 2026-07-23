#!/usr/bin/env python3
"""Pre-tag release readiness check over acceptance verdict history.

Runs every mechanical precondition from ``docs/release.md`` that must hold
BEFORE ``git tag v<version>``:

1. **Verdict history is green.** The two newest ``verdict-NNNN.json`` files
   in the history directory (written by ``scripts/acceptance_loop.py``) must
   both satisfy :meth:`acceptance.release.ReleaseGate.check_ready` —
   ``status == "EVALUATED"`` and ``all_pass is True``.
2. **At least one of those two verdicts came from ``eval_mode=full``.**
   A default-mode green is NOT release evidence for mode-gated tiers: in
   default mode the mode-gated criteria are excluded from the evaluated
   denominator, so a tier can report 100% while evaluating nothing (see the
   acceptance-loop doctrine skill). Verdicts written before ``eval_mode``
   was recorded count as not-full.
3. **CHANGELOG.md has a ``## <version>`` heading** for the version in
   ``pyproject.toml``.
4. **The version is not already tagged** — ``v<version>`` must not exist,
   i.e. the version bump landed.

Every failed check prints the exact command that fixes it and the script
exits nonzero. All checks are deterministic structural comparisons — no
heuristics, no semantic judgment.

Usage:
    python scripts/pre_tag_check.py
    python scripts/pre_tag_check.py --history-dir acceptance/verdict-history

Exit codes:
    0   ready to tag
    1   one or more preconditions failed
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT_DEFAULT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT_DEFAULT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT_DEFAULT))

from acceptance.release import ReleaseGate  # noqa: E402

#: Must match the naming scheme scripts/acceptance_loop.py writes.
_VERDICT_FILE_RE = re.compile(r"^verdict-(\d{4})\.json$")

_LOOP_CMD = "uv run python scripts/acceptance_loop.py --eval-mode full --iterations 2"


def find_newest_verdicts(history_dir: Path, count: int = 2) -> list[Path]:
    """Return the ``count`` newest verdict files, ordered oldest → newest."""
    if not history_dir.is_dir():
        return []
    matching = [p for p in history_dir.iterdir() if _VERDICT_FILE_RE.match(p.name)]
    return sorted(matching, key=lambda p: p.name)[-count:]


def read_pyproject_version(pyproject_path: Path) -> str:
    with pyproject_path.open("rb") as fh:
        data = tomllib.load(fh)
    version = (data.get("project") or {}).get("version")
    if not isinstance(version, str) or not version:
        raise ValueError(f"{pyproject_path} has no [project].version string")
    return version


def changelog_has_heading(changelog_path: Path, version: str) -> bool:
    if not changelog_path.is_file():
        return False
    heading = re.compile(rf"^##\s+{re.escape(version)}\b", re.MULTILINE)
    return heading.search(changelog_path.read_text()) is not None


def tag_exists(repo_root: Path, tag: str) -> bool:
    """Return True iff git reports ``tag`` as an existing tag."""
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "tag", "--list", tag],
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(completed.stdout.strip())


def _verdict_eval_mode(path: Path) -> str | None:
    """Read ``eval_mode`` from a verdict file; None on any parse problem."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    mode = data.get("eval_mode")
    return mode if isinstance(mode, str) else None


def run_checks(repo_root: Path, history_dir: Path) -> int:
    """Run all preconditions; print PASS/FAIL per check; return exit code."""
    failures: list[str] = []
    version = read_pyproject_version(repo_root / "pyproject.toml")
    print(f"pre-tag check for version {version}")

    # 1. Two green verdicts.
    verdict_paths = find_newest_verdicts(history_dir)
    if len(verdict_paths) < 2:
        failures.append(
            f"verdict history at {history_dir} has {len(verdict_paths)} "
            f"verdict file(s); two are required.\n"
            f"  Fix: run the acceptance loop first:\n    {_LOOP_CMD}"
        )
    elif not ReleaseGate(repo_root).check_ready(verdict_paths):
        details = []
        for path in verdict_paths:
            try:
                data = json.loads(path.read_text())
                details.append(
                    f"{path.name}: status={data.get('status')!r} "
                    f"all_pass={data.get('all_pass')!r}"
                )
            except (OSError, json.JSONDecodeError) as exc:
                details.append(f"{path.name}: unreadable ({exc})")
        failures.append(
            "the two newest verdicts are not both EVALUATED + all_pass:\n  "
            + "\n  ".join(details)
            + "\n  Fix: resolve the failures the verdicts report, then re-run:\n"
            f"    {_LOOP_CMD}"
        )
    else:
        print(f"PASS: last two verdicts EVALUATED + all_pass ({history_dir})")

    # 2. At least one of the two newest verdicts from eval_mode=full.
    if len(verdict_paths) >= 2:
        modes = [_verdict_eval_mode(p) for p in verdict_paths]
        if "full" not in modes:
            failures.append(
                f"neither of the two newest verdicts came from eval_mode=full "
                f"(modes: {modes}). A default-mode green is NOT release "
                "evidence for mode-gated tiers — their criteria are excluded "
                "from the evaluated denominator in default mode.\n"
                f"  Fix: {_LOOP_CMD}"
            )
        else:
            print("PASS: at least one of the last two verdicts is eval_mode=full")

    # 3. Changelog heading.
    if not changelog_has_heading(repo_root / "CHANGELOG.md", version):
        failures.append(
            f"CHANGELOG.md has no '## {version}' heading.\n"
            "  Fix: move the Unreleased content under a new "
            f"'## {version}' heading and commit it with the version bump."
        )
    else:
        print(f"PASS: CHANGELOG.md has a '## {version}' heading")

    # 4. Tag does not already exist.
    tag = f"v{version}"
    if tag_exists(repo_root, tag):
        failures.append(
            f"tag {tag} already exists — pyproject.toml still says "
            f"{version}, so the version bump has not landed.\n"
            "  Fix: bump [project].version in pyproject.toml "
            "(ReleaseGate.bump_version) and commit it with the CHANGELOG edit."
        )
    else:
        print(f"PASS: tag {tag} does not exist yet")

    sys.stdout.flush()
    if failures:
        print(f"\nNOT READY to tag {tag}:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print(f"\nREADY to tag {tag}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check every docs/release.md precondition before tagging."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT_DEFAULT,
        help="Repo root (default: parent of scripts/).",
    )
    parser.add_argument(
        "--history-dir",
        type=Path,
        default=None,
        help=(
            "Verdict history directory (default: "
            "<repo-root>/acceptance/verdict-history)."
        ),
    )
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    history_dir = (
        args.history_dir.resolve()
        if args.history_dir is not None
        else repo_root / "acceptance" / "verdict-history"
    )
    return run_checks(repo_root, history_dir)


if __name__ == "__main__":
    sys.exit(main())
