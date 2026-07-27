"""Subprocess coverage for the wrong-checkout collection guard."""

from __future__ import annotations

import os
import shutil
import site
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_decoy_codeprobe(
    tmp_path: Path,
    *,
    include_protocol: bool = True,
) -> Path:
    """Create a minimal codeprobe package outside this checkout."""
    decoy_root = tmp_path / "decoy_site_packages"
    package = decoy_root / "codeprobe"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    if include_protocol:
        adapters = package / "adapters"
        adapters.mkdir()
        (adapters / "__init__.py").write_text("")
        (adapters / "protocol.py").write_text(
            "class AdapterCapabilities:\n"
            "    pass\n\n\n"
            "class AgentConfig:\n"
            "    pass\n\n\n"
            "class AgentOutput:\n"
            "    pass\n"
        )
    return decoy_root


def _run_pytest_subprocess(
    cwd: Path,
    extra_pythonpath: Path,
    *pytest_args: str,
) -> subprocess.CompletedProcess[str]:
    """Run nested pytest without processing this venv's editable .pth."""
    pythonpath = os.pathsep.join(
        [str(extra_pythonpath), site.getsitepackages()[0]]
    )
    env = {**os.environ, "PYTHONPATH": pythonpath}
    return subprocess.run(
        [sys.executable, "-S", "-m", "pytest", *pytest_args, "-q"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _run_with_decoy_checkout(
    tmp_path: Path,
    *pytest_args: str,
    include_protocol: bool = True,
) -> subprocess.CompletedProcess[str]:
    decoy_root = _make_decoy_codeprobe(
        tmp_path,
        include_protocol=include_protocol,
    )
    return _run_pytest_subprocess(REPO_ROOT, decoy_root, *pytest_args)


@pytest.mark.integration
def test_targeted_run_aborts_on_wrong_checkout(
    tmp_path: Path,
) -> None:  # pragma: no cover - subprocess assertion
    result = _run_with_decoy_checkout(
        tmp_path,
        "tests/test_conftest_guard.py",
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "codeprobe was imported from" in result.stdout
    assert "uv sync --extra dev" in result.stdout


@pytest.mark.integration
def test_full_run_self_heals_and_does_not_false_abort(
    tmp_path: Path,
) -> None:  # pragma: no cover - subprocess assertion
    result = _run_with_decoy_checkout(
        tmp_path,
        "tests/mcp",
        "tests/test_conftest_guard.py",
    )

    assert "codeprobe was imported from" not in result.stdout, (
        result.stdout + result.stderr
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.integration
def test_targeted_run_aborts_with_remedy_when_protocol_module_missing(
    tmp_path: Path,
) -> None:  # pragma: no cover - subprocess assertion
    result = _run_with_decoy_checkout(
        tmp_path,
        "tests/test_conftest_guard.py",
        include_protocol=False,
    )
    combined_output = result.stdout + result.stderr

    assert result.returncode == 1, combined_output
    assert "codeprobe was imported from" in result.stdout
    assert "uv sync --extra dev" in result.stdout
    assert "ModuleNotFoundError" not in combined_output
    assert "ImportError while loading conftest" not in combined_output


def _make_correct_checkout_with_broken_protocol(tmp_path: Path) -> Path:
    """Copy the guard beside a genuinely broken local protocol module."""
    copy_root = tmp_path / "correct_checkout_copy"
    tests_dir = copy_root / "tests"
    adapters_dir = copy_root / "src" / "codeprobe" / "adapters"
    tests_dir.mkdir(parents=True)
    adapters_dir.mkdir(parents=True)
    (adapters_dir.parent / "__init__.py").write_text("")
    (adapters_dir / "__init__.py").write_text(
        (REPO_ROOT / "src/codeprobe/adapters/__init__.py").read_text()
    )

    real_protocol = (
        REPO_ROOT / "src/codeprobe/adapters/protocol.py"
    ).read_text()
    broken_protocol = real_protocol.replace(
        "class AgentConfig:",
        "class AgentConfigRenamedByAccident:",
        1,
    )
    assert broken_protocol != real_protocol
    (adapters_dir / "protocol.py").write_text(broken_protocol)

    shutil.copy(REPO_ROOT / "tests/conftest.py", tests_dir / "conftest.py")
    (tests_dir / "test_stub.py").write_text(
        "def test_stub():\n"
        "    assert True\n"
    )
    return copy_root


def _make_correct_checkout_with_broken_top_level_import(tmp_path: Path) -> Path:
    """Copy the guard beside a local codeprobe package that cannot import."""
    copy_root = tmp_path / "correct_checkout_copy"
    tests_dir = copy_root / "tests"
    package_dir = copy_root / "src" / "codeprobe"
    tests_dir.mkdir(parents=True)
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text(
        'raise ImportError("genuine top-level codeprobe import failure")\n'
    )

    shutil.copy(REPO_ROOT / "tests/conftest.py", tests_dir / "conftest.py")
    (tests_dir / "test_stub.py").write_text(
        "def test_stub():\n"
        "    assert True\n"
    )
    return copy_root


@pytest.mark.integration
def test_correct_checkout_reraises_when_protocol_import_genuinely_broken(
    tmp_path: Path,
) -> None:  # pragma: no cover - subprocess assertion
    copy_root = _make_correct_checkout_with_broken_protocol(tmp_path)

    result = _run_pytest_subprocess(
        copy_root,
        copy_root / "src",
        "tests/test_stub.py",
    )
    combined_output = result.stdout + result.stderr

    assert result.returncode != 0, combined_output
    assert "cannot import name 'AgentConfig'" in combined_output
    assert "codeprobe was imported from" not in result.stdout


@pytest.mark.integration
def test_correct_checkout_reraises_when_top_level_import_fails_and_module_is_evicted(
    tmp_path: Path,
) -> None:  # pragma: no cover - subprocess assertion
    copy_root = _make_correct_checkout_with_broken_top_level_import(tmp_path)

    result = _run_pytest_subprocess(
        copy_root,
        copy_root / "src",
        "tests/test_stub.py",
    )
    combined_output = result.stdout + result.stderr

    assert result.returncode != 0, combined_output
    assert "genuine top-level codeprobe import failure" in combined_output
    assert "codeprobe could not be resolved to a file location" not in combined_output
    assert "uv sync --extra dev" not in combined_output
