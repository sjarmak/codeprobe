"""Validate every task under examples/dual/*/* via run_validate.

Covers work unit u12-example-dual-tasks: ensures the shipped example
dual-verification tasks remain valid as the codebase evolves.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeprobe.cli.validate_cmd import run_validate

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_DIR = REPO_ROOT / "examples" / "dual"
CATEGORIES = ("comprehension", "sdlc")


def _discover_task_dirs() -> list[Path]:
    task_dirs: list[Path] = []
    for category in CATEGORIES:
        category_dir = EXAMPLES_DIR / category
        if not category_dir.is_dir():
            continue
        for child in sorted(category_dir.iterdir()):
            if child.is_dir():
                task_dirs.append(child)
    return task_dirs


TASK_DIRS = _discover_task_dirs()


def test_examples_dir_exists() -> None:
    assert EXAMPLES_DIR.is_dir(), f"missing {EXAMPLES_DIR}"
    assert (EXAMPLES_DIR / "README.md").is_file(), "missing README.md"


def test_minimum_task_counts() -> None:
    comprehension = [p for p in TASK_DIRS if p.parent.name == "comprehension"]
    sdlc = [p for p in TASK_DIRS if p.parent.name == "sdlc"]
    assert (
        len(comprehension) >= 10
    ), f"need >=10 comprehension tasks, found {len(comprehension)}"
    assert len(sdlc) >= 10, f"need >=10 sdlc tasks, found {len(sdlc)}"
    assert len(TASK_DIRS) >= 20, f"need >=20 total tasks, found {len(TASK_DIRS)}"


@pytest.mark.parametrize(
    "task_dir",
    TASK_DIRS,
    ids=[f"{p.parent.name}/{p.name}" for p in TASK_DIRS],
)
def test_example_task_validates(task_dir: Path) -> None:
    """Every example task must pass `codeprobe validate` cleanly."""
    results = run_validate(task_dir)
    failures = [r for r in results if not r.passed]
    assert (
        not failures
    ), f"validate failed for {task_dir.relative_to(REPO_ROOT)}: " + "; ".join(
        f"{r.name}: {r.detail}" for r in failures
    )


@pytest.mark.parametrize(
    "task_dir",
    TASK_DIRS,
    ids=[f"{p.parent.name}/{p.name}" for p in TASK_DIRS],
)
def test_example_task_is_dual_mode(task_dir: Path) -> None:
    """Every example task's metadata must declare verification_mode='dual'."""
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]

    toml_path = task_dir / "task.toml"
    assert toml_path.is_file(), f"missing task.toml in {task_dir}"
    with open(toml_path, "rb") as f:
        data = tomllib.load(f)
    verification = data.get("verification", {})
    assert verification.get("verification_mode") == "dual", (
        f"{task_dir.name}: verification_mode is "
        f"{verification.get('verification_mode')!r}, expected 'dual'"
    )
