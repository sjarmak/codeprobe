"""Regression tests for wheel/sdist packaging completeness.

These guard against shipping a wheel that is missing runtime-loaded data
files. The original incident: ``codeprobe/preambles/templates/mcp_base.md.j2``
was loaded at runtime by ``FileSystemLoader`` but the
``codeprobe.preambles.templates`` sub-package was not declared in
``[tool.setuptools.package-data]``, so ``pip install codeprobe`` produced an
import that crashed with ``TemplateNotFound`` on the MCP mining flow.

Two layers of defense:

1. ``test_runtime_data_files_declared_in_package_data`` (fast, default-on):
   walks ``src/codeprobe/`` for every data file with a runtime-relevant
   extension and asserts a ``package-data`` glob in ``pyproject.toml``
   would match it. Catches the "added a new template under a new
   sub-package and forgot to update pyproject.toml" regression class
   without paying for a wheel build.

2. ``test_built_wheel_contains_runtime_data_files`` (integration, opt-in):
   actually builds a wheel and inspects the zip for the same files.
   Authoritative but slow (~20-30s); marked ``integration`` and run only
   when explicitly requested.
"""

from __future__ import annotations

import fnmatch
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src" / "codeprobe"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# File extensions that are loaded at runtime via ``Path(__file__).parent``,
# ``FileSystemLoader``, or ``importlib.resources``. If a file with one of
# these extensions lives inside a Python sub-package, it MUST be declared
# in ``[tool.setuptools.package-data]`` or it will be missing from the wheel.
RUNTIME_DATA_SUFFIXES: tuple[str, ...] = (
    ".j2",
    ".md.j2",
    ".yaml",
    ".yml",
    ".json",
)


def _is_python_package(directory: Path) -> bool:
    return (directory / "__init__.py").is_file()


def _package_dotted_name(directory: Path) -> str:
    rel = directory.relative_to(SRC_ROOT.parent)
    return ".".join(rel.parts)


def _iter_runtime_data_files() -> list[Path]:
    """Yield every runtime data file that lives inside a Python sub-package."""
    found: list[Path] = []
    for path in SRC_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts:
            continue
        if not any(path.name.endswith(suffix) for suffix in RUNTIME_DATA_SUFFIXES):
            continue
        # Files at the package root must be inside an actual package.
        if not _is_python_package(path.parent):
            continue
        found.append(path)
    return found


def _load_package_data() -> dict[str, list[str]]:
    raw = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return raw.get("tool", {}).get("setuptools", {}).get("package-data", {})


def _glob_matches(pattern: str, name: str) -> bool:
    return fnmatch.fnmatchcase(name, pattern)


def test_runtime_data_files_declared_in_package_data() -> None:
    """Every runtime data file under src/codeprobe/ is reachable via a
    declared package-data glob."""
    package_data = _load_package_data()
    files = _iter_runtime_data_files()
    assert files, "expected to find at least one runtime data file"

    missing: list[str] = []
    for data_file in files:
        pkg_name = _package_dotted_name(data_file.parent)
        globs = package_data.get(pkg_name)
        if not globs:
            missing.append(
                f"{data_file.relative_to(REPO_ROOT)}: package "
                f"'{pkg_name}' has no [tool.setuptools.package-data] entry"
            )
            continue
        if not any(_glob_matches(g, data_file.name) for g in globs):
            missing.append(
                f"{data_file.relative_to(REPO_ROOT)}: no glob in "
                f"package-data['{pkg_name}'] = {globs!r} matches "
                f"'{data_file.name}'"
            )

    assert not missing, (
        "Runtime data files are not covered by [tool.setuptools.package-data]:\n  "
        + "\n  ".join(missing)
    )


@pytest.mark.integration
def test_built_wheel_contains_runtime_data_files(tmp_path: Path) -> None:
    """Build a wheel and assert it ships every runtime data file.

    Slow (~20-30s); opt in with::

        pytest tests/test_packaging.py -m integration
    """
    if shutil.which("python") is None:  # pragma: no cover - defensive
        pytest.skip("python not on PATH")

    dist_dir = tmp_path / "dist"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--outdir",
            str(dist_dir),
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )

    wheels = list(dist_dir.glob("codeprobe-*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, found {wheels}"

    with zipfile.ZipFile(wheels[0]) as zf:
        names = set(zf.namelist())

    expected = [
        # path inside the wheel, relative to the package root
        str(p.relative_to(SRC_ROOT.parent)).replace("\\", "/")
        for p in _iter_runtime_data_files()
    ]
    missing = [name for name in expected if name not in names]
    assert not missing, f"wheel is missing data files: {missing}"
