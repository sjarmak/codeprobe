"""Release gate for the acceptance loop.

Gatekeeps release by combining deterministic verdict-history checks with a
local wheel staging step (build → install in fresh venv → smoke test → run a
handful of structural acceptance criteria against the installed copy).

Responsibilities:

- :meth:`ReleaseGate.check_ready` — read the two most recent verdict.json
  files; returns ``True`` only if both are ``EVALUATED`` and ``all_pass``.
- :meth:`ReleaseGate.build_and_stage` — ``python -m build --wheel``, create a
  throw-away venv, ``pip install`` the wheel, run ``codeprobe --version``,
  assert the version matches ``pyproject.toml``, then run the first five
  structural criteria against an empty workspace.
- :meth:`ReleaseGate.bump_version` — increment the ``version = "X.Y.Z"``
  line in ``pyproject.toml``. Uses ``tomllib`` to read and line-level string
  replacement to write, avoiding a toml-writer dependency.
- :meth:`ReleaseGate.prepare_tag` — return ``f"v{version}"``. **Does not**
  create or push the tag — the calling skill decides when to do that.

This module is deliberately ZFC-compliant: every decision is a deterministic
arithmetic or string check, and all subprocess invocations fail loudly
rather than masking errors.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from acceptance.loader import filter_by_tier
from acceptance.verify import STATUS_EVALUATED, Verifier

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Number of structural criteria to exercise against the freshly-staged
#: install. Matches R13 in the PRD — "5 structural criteria".
STRUCTURAL_SMOKE_COUNT: int = 5

#: Default staging root for build_and_stage. Placed under /tmp so CI and
#: local runs cannot accidentally pollute the repo tree.
DEFAULT_STAGE_ROOT: Path = Path(tempfile.gettempdir()) / "codeprobe-release-stage"

BumpType = Literal["major", "minor", "patch"]
_BUMP_TYPES: frozenset[str] = frozenset({"major", "minor", "patch"})


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StagingResult:
    """Immutable result of a local wheel-staging cycle.

    Every boolean is ``True`` only when the corresponding step completed
    successfully. ``error`` is ``None`` on success or a short human-readable
    string when any step fails. ``wheel_path`` is populated as soon as the
    wheel has been located under ``dist/`` — callers should check ``built``
    rather than ``wheel_path is not None`` for a cleaner success gate.
    """

    built: bool
    installed: bool
    version_matches: bool
    structural_criteria_passed: bool
    wheel_path: Path | None
    error: str | None


# ---------------------------------------------------------------------------
# Release gate
# ---------------------------------------------------------------------------


class ReleaseGate:
    """Release gate combining verdict history + local wheel smoke test.

    Args:
        repo_root: Path to the codeprobe repository root. Used to locate
            ``pyproject.toml``, the ``dist/`` directory, and the
            ``acceptance/criteria.toml`` manifest.
        pyproject_path: Optional override for ``repo_root / "pyproject.toml"``.
            Useful in tests that want to point at a tmp-path copy.
        criteria_path: Optional override for
            ``repo_root / "acceptance/criteria.toml"``. Useful in tests.
        stage_root: Optional override for the staging directory. Defaults to
            :data:`DEFAULT_STAGE_ROOT`.
    """

    def __init__(
        self,
        repo_root: Path,
        pyproject_path: Path | None = None,
        criteria_path: Path | None = None,
        stage_root: Path | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.pyproject_path = Path(
            pyproject_path or (self.repo_root / "pyproject.toml")
        )
        self.criteria_path = Path(
            criteria_path or (self.repo_root / "acceptance" / "criteria.toml")
        )
        self.stage_root = Path(stage_root or DEFAULT_STAGE_ROOT)

    # ------------------------------------------------------------ check_ready

    def check_ready(self, verdict_paths: list[Path]) -> bool:
        """Return True iff the last two verdicts are both fully passing.

        Args:
            verdict_paths: Ordered list (oldest → newest) of paths to
                ``verdict.json`` files. Fewer than two entries is treated as
                "not ready" rather than an error — the caller can keep
                iterating the acceptance loop.

        Returns:
            ``True`` iff both ``verdict_paths[-1]`` and ``verdict_paths[-2]``
            load cleanly, have ``status == "EVALUATED"``, and
            ``all_pass is True``. ``False`` in every other case — including
            JSON parse errors, missing files, or ``INCOMPLETE`` status.
        """
        if len(verdict_paths) < 2:
            return False
        last_two = verdict_paths[-2:]
        for path in last_two:
            verdict = _load_verdict(Path(path))
            if verdict is None:
                return False
            if verdict.get("status") != STATUS_EVALUATED:
                return False
            if not verdict.get("all_pass", False):
                return False
        return True

    # -------------------------------------------------------- build_and_stage

    def build_and_stage(self) -> StagingResult:
        """Build a wheel, install it in a fresh venv, and smoke test.

        Steps:

        1. ``python -m build --wheel`` in :attr:`repo_root`.
        2. Locate the produced wheel under ``dist/``.
        3. Create a fresh venv under :attr:`stage_root`.
        4. ``pip install`` the wheel into the venv.
        5. Run ``codeprobe --version`` and parse the version.
        6. Compare the reported version to ``pyproject.toml``.
        7. Run the first :data:`STRUCTURAL_SMOKE_COUNT` structural criteria
           from ``acceptance/criteria.toml`` against an empty workspace.

        Any failing step short-circuits the rest — later fields in the
        returned :class:`StagingResult` remain ``False`` / ``None`` and the
        ``error`` field summarises what broke.
        """
        # Step 1: build wheel
        try:
            self._run_subprocess(
                [sys.executable, "-m", "build", "--wheel"],
                cwd=self.repo_root,
            )
        except subprocess.CalledProcessError as exc:
            return StagingResult(
                built=False,
                installed=False,
                version_matches=False,
                structural_criteria_passed=False,
                wheel_path=None,
                error=f"build failed: {exc}",
            )

        wheel_path = self._latest_wheel()
        if wheel_path is None:
            return StagingResult(
                built=False,
                installed=False,
                version_matches=False,
                structural_criteria_passed=False,
                wheel_path=None,
                error="no wheel found under dist/ after build",
            )

        # Step 2: fresh venv
        venv_dir = self.stage_root / "venv"
        if self.stage_root.exists():
            shutil.rmtree(self.stage_root, ignore_errors=True)
        self.stage_root.mkdir(parents=True, exist_ok=True)
        try:
            self._run_subprocess(
                [sys.executable, "-m", "venv", str(venv_dir)],
                cwd=self.repo_root,
            )
        except subprocess.CalledProcessError as exc:
            return StagingResult(
                built=True,
                installed=False,
                version_matches=False,
                structural_criteria_passed=False,
                wheel_path=wheel_path,
                error=f"venv creation failed: {exc}",
            )

        venv_python = venv_dir / "bin" / "python"
        venv_codeprobe = venv_dir / "bin" / "codeprobe"

        # Step 3: pip install the wheel
        try:
            self._run_subprocess(
                [str(venv_python), "-m", "pip", "install", str(wheel_path)],
                cwd=self.repo_root,
            )
        except subprocess.CalledProcessError as exc:
            return StagingResult(
                built=True,
                installed=False,
                version_matches=False,
                structural_criteria_passed=False,
                wheel_path=wheel_path,
                error=f"pip install failed: {exc}",
            )

        # Step 4: codeprobe --version + compare
        try:
            completed = self._run_subprocess(
                [str(venv_codeprobe), "--version"],
                cwd=self.repo_root,
                capture_output=True,
            )
            reported = (completed.stdout or "").strip()
        except subprocess.CalledProcessError as exc:
            return StagingResult(
                built=True,
                installed=True,
                version_matches=False,
                structural_criteria_passed=False,
                wheel_path=wheel_path,
                error=f"codeprobe --version failed: {exc}",
            )

        pyproject_version = self._read_pyproject_version()
        version_matches = pyproject_version in reported
        if not version_matches:
            return StagingResult(
                built=True,
                installed=True,
                version_matches=False,
                structural_criteria_passed=False,
                wheel_path=wheel_path,
                error=(
                    f"version mismatch: pyproject={pyproject_version!r}, "
                    f"reported={reported!r}"
                ),
            )

        # Step 5: run five structural criteria against an empty workspace
        try:
            criteria_passed = self._run_structural_smoke()
        except Exception as exc:  # pragma: no cover - defensive
            return StagingResult(
                built=True,
                installed=True,
                version_matches=True,
                structural_criteria_passed=False,
                wheel_path=wheel_path,
                error=f"structural smoke failed: {exc}",
            )

        if not criteria_passed:
            return StagingResult(
                built=True,
                installed=True,
                version_matches=True,
                structural_criteria_passed=False,
                wheel_path=wheel_path,
                error="one or more structural smoke criteria did not pass",
            )

        return StagingResult(
            built=True,
            installed=True,
            version_matches=True,
            structural_criteria_passed=True,
            wheel_path=wheel_path,
            error=None,
        )

    # ------------------------------------------------------------ bump_version

    def bump_version(self, bump_type: BumpType = "patch") -> str:
        """Increment the ``version`` field in ``pyproject.toml``.

        Args:
            bump_type: ``"major"``, ``"minor"``, or ``"patch"``. Defaults to
                ``"patch"``.

        Returns:
            The new version string (e.g. ``"0.4.2"``).

        Raises:
            ValueError: ``bump_type`` is not recognised or the current version
                in ``pyproject.toml`` is not a valid ``X.Y.Z`` triple.
        """
        if bump_type not in _BUMP_TYPES:
            raise ValueError(
                f"unknown bump_type {bump_type!r}; expected one of "
                f"{sorted(_BUMP_TYPES)}"
            )

        current = self._read_pyproject_version()
        parts = current.split(".")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise ValueError(
                f"pyproject.toml version {current!r} is not a valid X.Y.Z triple"
            )
        major, minor, patch = (int(p) for p in parts)

        if bump_type == "major":
            new_version = f"{major + 1}.0.0"
        elif bump_type == "minor":
            new_version = f"{major}.{minor + 1}.0"
        else:
            new_version = f"{major}.{minor}.{patch + 1}"

        self._write_pyproject_version(current, new_version)
        return new_version

    # ------------------------------------------------------------- prepare_tag

    def prepare_tag(self, version: str) -> str:
        """Return the git tag string for ``version`` without creating it.

        Dry-run safe — this is a pure function that never touches git. The
        orchestrating skill is responsible for deciding when (and whether)
        to actually create and push the tag.
        """
        return f"v{version}"

    # -------------------------------------------------------------- internals

    def _read_pyproject_version(self) -> str:
        with self.pyproject_path.open("rb") as fh:
            data = tomllib.load(fh)
        project = data.get("project") or {}
        version = project.get("version")
        if not isinstance(version, str) or not version:
            raise ValueError(
                f"pyproject.toml at {self.pyproject_path} has no "
                f"[project].version string"
            )
        return version

    def _write_pyproject_version(self, old: str, new: str) -> None:
        """Rewrite the ``version = "X"`` line in-place.

        Only the first line matching ``version = "<old>"`` in the
        ``[project]`` section is replaced so comments and other version-
        looking strings elsewhere in the file are left untouched.
        """
        text = self.pyproject_path.read_text()
        lines = text.splitlines(keepends=True)
        in_project = False
        replaced = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                in_project = stripped == "[project]"
                continue
            if in_project and stripped.startswith("version"):
                # Expect: version = "0.4.1"
                if f'"{old}"' in line:
                    lines[i] = line.replace(f'"{old}"', f'"{new}"', 1)
                    replaced = True
                    break
        if not replaced:
            raise ValueError(
                f'could not locate version = "{old}" line in ' f"{self.pyproject_path}"
            )
        self.pyproject_path.write_text("".join(lines))

    def _latest_wheel(self) -> Path | None:
        dist_dir = self.repo_root / "dist"
        if not dist_dir.is_dir():
            return None
        wheels = sorted(
            dist_dir.glob("codeprobe-*.whl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return wheels[0] if wheels else None

    def _run_subprocess(
        self,
        cmd: list[str],
        cwd: Path,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """Run a subprocess and raise on non-zero exit.

        Thin wrapper around ``subprocess.run`` that always passes
        ``check=True`` and ``text=True``. Extracted so tests can mock a
        single method instead of patching ``subprocess.run`` globally.
        """
        return subprocess.run(  # noqa: S603 - inputs are constructed from trusted args
            cmd,
            cwd=str(cwd),
            check=True,
            text=True,
            capture_output=capture_output,
        )

    def _run_structural_smoke(self) -> bool:
        """Run :data:`STRUCTURAL_SMOKE_COUNT` structural criteria.

        Returns ``True`` iff every selected criterion evaluated to pass. A
        single fail or skip returns ``False`` — the smoke test is
        deliberately strict because it runs against a fresh install where
        all structural checks should be deterministic.
        """
        if not self.criteria_path.is_file():
            return False
        verifier = Verifier(
            criteria_path=self.criteria_path, project_root=self.repo_root
        )
        structural = filter_by_tier(verifier.criteria, "structural")
        selected = structural[:STRUCTURAL_SMOKE_COUNT]
        if not selected:
            return False
        # Run each selected handler directly so we do not spuriously fail on
        # unrelated criteria in the rest of the manifest.
        handlers = Verifier._handlers()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            for criterion in selected:
                handler = handlers.get(criterion.check_type)
                if handler is None:
                    return False
                result = handler(verifier, criterion, workspace)
                if result.result != "pass":
                    return False
        return True


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _load_verdict(path: Path) -> dict | None:
    """Load and JSON-parse a verdict file, returning ``None`` on any error."""
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


__all__ = [
    "DEFAULT_STAGE_ROOT",
    "STRUCTURAL_SMOKE_COUNT",
    "ReleaseGate",
    "StagingResult",
]
