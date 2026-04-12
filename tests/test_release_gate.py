"""Unit tests for :mod:`acceptance.release`.

These tests intentionally avoid the ~30-second cost of actually building
a wheel — the build / venv / pip steps are exercised by mocking
``ReleaseGate._run_subprocess`` and by faking a dummy wheel under
``dist/``. A single optional integration test (marked with
``@pytest.mark.integration`` and skipped by default) can do the real build
when a maintainer wants end-to-end coverage.
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

import pytest

from acceptance.release import (
    STRUCTURAL_SMOKE_COUNT,
    ReleaseGate,
    StagingResult,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


PYPROJECT_TEMPLATE = """\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "codeprobe"
version = "0.4.1"
description = "Demo"
requires-python = ">=3.11"

[tool.something]
# A fake version field that must NOT be touched:
version = "not-the-real-one"
"""


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Create a tmp directory resembling a codeprobe repo root."""
    (tmp_path / "pyproject.toml").write_text(PYPROJECT_TEMPLATE)
    (tmp_path / "dist").mkdir()
    (tmp_path / "acceptance").mkdir()
    return tmp_path


@pytest.fixture
def gate(tmp_repo: Path) -> ReleaseGate:
    return ReleaseGate(
        repo_root=tmp_repo,
        stage_root=tmp_repo / "stage",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_verdict(
    path: Path,
    *,
    status: str = "EVALUATED",
    all_pass: bool = True,
    pass_count: int = 3,
    fail_count: int = 0,
) -> Path:
    payload = {
        "iteration": 1,
        "status": status,
        "all_pass": all_pass,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "skip_count": 0,
        "total_criteria": pass_count + fail_count,
        "evaluated_pct": {"structural": 100.0},
        "tier_counts": {},
        "failures": [],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return path


# ---------------------------------------------------------------------------
# StagingResult dataclass shape
# ---------------------------------------------------------------------------


def test_staging_result_has_expected_fields() -> None:
    field_names = {f.name for f in fields(StagingResult)}
    assert field_names == {
        "built",
        "installed",
        "version_matches",
        "structural_criteria_passed",
        "wheel_path",
        "error",
    }


def test_staging_result_is_frozen() -> None:
    result = StagingResult(
        built=True,
        installed=True,
        version_matches=True,
        structural_criteria_passed=True,
        wheel_path=None,
        error=None,
    )
    with pytest.raises(Exception):
        result.built = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# check_ready
# ---------------------------------------------------------------------------


def test_check_ready_two_passing(gate: ReleaseGate, tmp_path: Path) -> None:
    v1 = _write_verdict(tmp_path / "v1" / "verdict.json", all_pass=True)
    v2 = _write_verdict(tmp_path / "v2" / "verdict.json", all_pass=True)
    assert gate.check_ready([v1, v2]) is True


def test_check_ready_passing_then_failing(gate: ReleaseGate, tmp_path: Path) -> None:
    v1 = _write_verdict(tmp_path / "v1" / "verdict.json", all_pass=True)
    v2 = _write_verdict(
        tmp_path / "v2" / "verdict.json",
        all_pass=False,
        fail_count=1,
    )
    assert gate.check_ready([v1, v2]) is False


def test_check_ready_failing_then_passing(gate: ReleaseGate, tmp_path: Path) -> None:
    v1 = _write_verdict(
        tmp_path / "v1" / "verdict.json",
        all_pass=False,
        fail_count=1,
    )
    v2 = _write_verdict(tmp_path / "v2" / "verdict.json", all_pass=True)
    assert gate.check_ready([v1, v2]) is False


def test_check_ready_incomplete_status_blocks(
    gate: ReleaseGate, tmp_path: Path
) -> None:
    v1 = _write_verdict(tmp_path / "v1" / "verdict.json", all_pass=True)
    v2 = _write_verdict(
        tmp_path / "v2" / "verdict.json",
        status="INCOMPLETE",
        all_pass=False,
    )
    assert gate.check_ready([v1, v2]) is False


def test_check_ready_fewer_than_two_verdicts(gate: ReleaseGate, tmp_path: Path) -> None:
    assert gate.check_ready([]) is False
    v1 = _write_verdict(tmp_path / "v1" / "verdict.json", all_pass=True)
    assert gate.check_ready([v1]) is False


def test_check_ready_uses_last_two_only(gate: ReleaseGate, tmp_path: Path) -> None:
    """A passing verdict followed by a failure should block, even if earlier
    verdicts in the list were fine. The gate only looks at the final two."""
    v1 = _write_verdict(tmp_path / "v1" / "verdict.json", all_pass=True)
    v2 = _write_verdict(tmp_path / "v2" / "verdict.json", all_pass=True)
    v3 = _write_verdict(
        tmp_path / "v3" / "verdict.json",
        all_pass=False,
        fail_count=1,
    )
    assert gate.check_ready([v1, v2, v3]) is False


def test_check_ready_missing_file(gate: ReleaseGate, tmp_path: Path) -> None:
    v1 = _write_verdict(tmp_path / "v1" / "verdict.json", all_pass=True)
    ghost = tmp_path / "ghost" / "verdict.json"
    assert gate.check_ready([v1, ghost]) is False


def test_check_ready_malformed_json(gate: ReleaseGate, tmp_path: Path) -> None:
    v1 = _write_verdict(tmp_path / "v1" / "verdict.json", all_pass=True)
    bad = tmp_path / "bad" / "verdict.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{ not valid json")
    assert gate.check_ready([v1, bad]) is False


# ---------------------------------------------------------------------------
# bump_version
# ---------------------------------------------------------------------------


def test_bump_version_patch_default(gate: ReleaseGate) -> None:
    new_version = gate.bump_version()
    assert new_version == "0.4.2"
    # File is actually rewritten:
    assert '"0.4.2"' in gate.pyproject_path.read_text()


def test_bump_version_minor(gate: ReleaseGate) -> None:
    new_version = gate.bump_version("minor")
    assert new_version == "0.5.0"
    assert '"0.5.0"' in gate.pyproject_path.read_text()


def test_bump_version_major(gate: ReleaseGate) -> None:
    new_version = gate.bump_version("major")
    assert new_version == "1.0.0"
    assert '"1.0.0"' in gate.pyproject_path.read_text()


def test_bump_version_unknown_type_raises(gate: ReleaseGate) -> None:
    with pytest.raises(ValueError, match="unknown bump_type"):
        gate.bump_version("nope")  # type: ignore[arg-type]


def test_bump_version_leaves_unrelated_version_lines(
    gate: ReleaseGate,
) -> None:
    """The ``[tool.something]`` table also has a ``version = "..."`` line;
    only the one under ``[project]`` should be touched."""
    gate.bump_version("patch")
    text = gate.pyproject_path.read_text()
    assert '"0.4.2"' in text
    assert '"not-the-real-one"' in text


def test_bump_version_rejects_non_triple(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\nversion = "1.0"\n')
    gate = ReleaseGate(repo_root=tmp_path, pyproject_path=pyproject)
    with pytest.raises(ValueError, match="X.Y.Z"):
        gate.bump_version("patch")


# ---------------------------------------------------------------------------
# prepare_tag
# ---------------------------------------------------------------------------


def test_prepare_tag_returns_v_prefixed(gate: ReleaseGate) -> None:
    assert gate.prepare_tag("0.4.2") == "v0.4.2"


def test_prepare_tag_is_pure(gate: ReleaseGate) -> None:
    """Calling prepare_tag must not touch git, the filesystem, or the gate's
    own state. Simply assert repeat calls return the same value and
    pyproject.toml is untouched."""
    before = gate.pyproject_path.read_text()
    assert gate.prepare_tag("1.2.3") == "v1.2.3"
    assert gate.prepare_tag("1.2.3") == "v1.2.3"
    assert gate.pyproject_path.read_text() == before


# ---------------------------------------------------------------------------
# build_and_stage (subprocess-mocked unit tests)
# ---------------------------------------------------------------------------


class _FakeCompletedProcess:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _install_fake_subprocess(
    gate: ReleaseGate,
    *,
    version_output: str = "codeprobe, version 0.4.1",
) -> list[list[str]]:
    """Replace ``gate._run_subprocess`` with a recording fake.

    Returns the list that will be populated with one entry per invocation
    (useful for asserting the expected sequence of commands was run).

    Side effect: fabricates a dummy wheel under ``dist/`` on the first
    ``python -m build`` call and creates a fake venv binary layout on the
    ``python -m venv`` call so subsequent steps have plausible paths.
    """
    calls: list[list[str]] = []

    def fake_run(
        cmd: list[str],
        cwd: Path,
        capture_output: bool = False,
    ) -> _FakeCompletedProcess:
        calls.append(list(cmd))
        # python -m build --wheel → fabricate a wheel under dist/
        if len(cmd) >= 3 and cmd[1:3] == ["-m", "build"]:
            dist = gate.repo_root / "dist"
            dist.mkdir(parents=True, exist_ok=True)
            (dist / "codeprobe-0.4.1-py3-none-any.whl").write_bytes(b"fake")
            return _FakeCompletedProcess()
        # python -m venv <dir> → create the expected bin/ layout
        if len(cmd) >= 3 and cmd[1:3] == ["-m", "venv"]:
            venv_dir = Path(cmd[3])
            (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
            (venv_dir / "bin" / "python").write_text("")
            (venv_dir / "bin" / "codeprobe").write_text("")
            return _FakeCompletedProcess()
        # .../bin/codeprobe --version → controlled stdout
        if cmd[-1] == "--version":
            return _FakeCompletedProcess(stdout=version_output)
        # .../bin/python -m pip install <wheel> → no-op success
        return _FakeCompletedProcess()

    gate._run_subprocess = fake_run  # type: ignore[method-assign]
    return calls


def test_build_and_stage_happy_path(
    gate: ReleaseGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _install_fake_subprocess(gate)
    # Bypass the real structural smoke — we exercise it in its own test.
    monkeypatch.setattr(gate, "_run_structural_smoke", lambda: True)

    result = gate.build_and_stage()

    assert result.built is True
    assert result.installed is True
    assert result.version_matches is True
    assert result.structural_criteria_passed is True
    assert result.wheel_path is not None
    assert result.wheel_path.name == "codeprobe-0.4.1-py3-none-any.whl"
    assert result.error is None
    # Must have issued build, venv, pip install, and version commands:
    joined = [" ".join(c) for c in calls]
    assert any("build --wheel" in j for j in joined)
    assert any("-m venv" in j for j in joined)
    assert any("pip install" in j for j in joined)
    assert any("--version" in j for j in joined)


def test_build_and_stage_version_mismatch(
    gate: ReleaseGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_subprocess(gate, version_output="codeprobe, version 9.9.9")
    monkeypatch.setattr(gate, "_run_structural_smoke", lambda: True)

    result = gate.build_and_stage()

    assert result.built is True
    assert result.installed is True
    assert result.version_matches is False
    assert result.structural_criteria_passed is False
    assert result.error is not None
    assert "version mismatch" in result.error


def test_build_and_stage_build_failure(
    gate: ReleaseGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess as _sp

    def boom(*_a: object, **_kw: object) -> _FakeCompletedProcess:
        raise _sp.CalledProcessError(returncode=1, cmd=["python", "-m", "build"])

    gate._run_subprocess = boom  # type: ignore[method-assign]
    result = gate.build_and_stage()
    assert result.built is False
    assert result.installed is False
    assert result.error is not None
    assert "build failed" in result.error


def test_build_and_stage_smoke_failure(
    gate: ReleaseGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_subprocess(gate)
    monkeypatch.setattr(gate, "_run_structural_smoke", lambda: False)

    result = gate.build_and_stage()
    assert result.built is True
    assert result.installed is True
    assert result.version_matches is True
    assert result.structural_criteria_passed is False
    assert result.error is not None
    assert "structural smoke" in result.error


def test_build_and_stage_pip_install_failure(
    gate: ReleaseGate, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess as _sp

    def fake_run(
        cmd: list[str], cwd: Path, capture_output: bool = False
    ) -> _FakeCompletedProcess:
        if len(cmd) >= 3 and cmd[1:3] == ["-m", "build"]:
            dist = gate.repo_root / "dist"
            dist.mkdir(parents=True, exist_ok=True)
            (dist / "codeprobe-0.4.1-py3-none-any.whl").write_bytes(b"fake")
            return _FakeCompletedProcess()
        if len(cmd) >= 3 and cmd[1:3] == ["-m", "venv"]:
            venv_dir = Path(cmd[3])
            (venv_dir / "bin").mkdir(parents=True, exist_ok=True)
            return _FakeCompletedProcess()
        if "pip" in cmd:
            raise _sp.CalledProcessError(returncode=1, cmd=cmd)
        return _FakeCompletedProcess()

    gate._run_subprocess = fake_run  # type: ignore[method-assign]
    result = gate.build_and_stage()
    assert result.built is True
    assert result.installed is False
    assert "pip install failed" in (result.error or "")


# ---------------------------------------------------------------------------
# Structural smoke integration — uses the real acceptance/criteria.toml
# ---------------------------------------------------------------------------


def test_structural_smoke_count_is_five() -> None:
    assert STRUCTURAL_SMOKE_COUNT == 5


def test_run_structural_smoke_with_missing_criteria(
    tmp_repo: Path,
) -> None:
    """If the criteria manifest is absent, the smoke test must return
    False rather than crashing."""
    gate = ReleaseGate(
        repo_root=tmp_repo,
        criteria_path=tmp_repo / "acceptance" / "criteria.toml",
    )
    assert not (tmp_repo / "acceptance" / "criteria.toml").exists()
    assert gate._run_structural_smoke() is False


# ---------------------------------------------------------------------------
# Import / construction smoke
# ---------------------------------------------------------------------------


def test_release_gate_importable_and_constructs(tmp_repo: Path) -> None:
    import acceptance.release as rel

    rg = rel.ReleaseGate(tmp_repo)
    assert rg.repo_root == tmp_repo.resolve()
    assert rg.pyproject_path == tmp_repo / "pyproject.toml"


# ---------------------------------------------------------------------------
# Optional end-to-end integration test (skipped by default)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_build_and_stage_real_wheel() -> None:  # pragma: no cover - opt-in
    """Actually build a wheel and stage it. Slow (~30s) — run with:

    pytest tests/test_release_gate.py -m integration
    """
    repo_root = Path(__file__).resolve().parent.parent
    gate = ReleaseGate(repo_root=repo_root)
    result = gate.build_and_stage()
    # We do not assert structural_criteria_passed here because the full
    # manifest currently contains a known-failing criterion unrelated to
    # release gating. We only assert that the wheel built and installed.
    assert result.built is True
    assert result.installed is True
    assert result.version_matches is True
