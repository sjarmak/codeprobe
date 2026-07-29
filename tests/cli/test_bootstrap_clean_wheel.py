"""Installed-wheel integration coverage for containment image bootstrap."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENT_DIGEST = "sha256:" + "a" * 64
_SCORING_DIGEST = "sha256:" + "b" * 64
_AGENT_LOCAL_ID = "sha256:" + "c" * 64
_SCORING_LOCAL_ID = "sha256:" + "d" * 64
_AGENT_REF = f"registry.example.test/platform/codeprobe-agent@{_AGENT_DIGEST}"
_SCORING_REF = f"registry.example.test/platform/codeprobe-scoring@{_SCORING_DIGEST}"


@pytest.mark.integration
def test_installed_wheel_bootstraps_without_checkout_or_dockerfiles(
    tmp_path: Path,
) -> None:
    venv_dir = _install_wheel(_build_wheel(tmp_path), tmp_path / "venv")
    venv_python = venv_dir / "bin" / "python"
    run_dir = tmp_path / "outside-checkout"
    run_dir.mkdir()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    engine_log = tmp_path / "engine.log"
    _write_fake_engine(fake_bin / "docker")
    config_path = run_dir / "container-images.json"
    env = _engine_environment(fake_bin, engine_log)
    env["CODEPROBE_CONTAINER_CONFIG"] = str(config_path)
    installed_path = _installed_package_path(venv_python, run_dir, env)
    result = _run_installed_bootstrap(venv_dir, run_dir, config_path, env)

    assert not Path(installed_path).is_relative_to(REPO_ROOT)
    assert result.returncode == 0, result.stderr or result.stdout
    assert _AGENT_LOCAL_ID in result.stdout
    assert _SCORING_LOCAL_ID in result.stdout
    assert config_path.is_file()
    engine_calls = engine_log.read_text(encoding="utf-8")
    assert "image pull" in engine_calls
    assert "image inspect" in engine_calls
    assert str(REPO_ROOT) not in engine_calls
    assert "Dockerfile" not in engine_calls


@pytest.mark.integration
def test_installed_wheel_bare_bootstrap_reports_image_config_errors_before_engine_work(
    tmp_path: Path,
) -> None:
    venv_dir = _install_wheel(_build_wheel(tmp_path), tmp_path / "venv")
    run_dir = tmp_path / "outside-checkout"
    run_dir.mkdir()
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_fake_engine(fake_bin / "docker")
    cases = (
        (
            {"CODEPROBE_IMAGE_REGISTRY": "registry.example.test"},
            "Missing required image setting(s): CODEPROBE_IMAGE_NAMESPACE",
        ),
        (
            {
                "CODEPROBE_IMAGE_REGISTRY": "REGISTRY.example.test",
                "CODEPROBE_IMAGE_NAMESPACE": "platform/codeprobe",
            },
            "CODEPROBE_IMAGE_REGISTRY has an invalid registry host",
        ),
    )

    for index, (image_env, expected) in enumerate(cases):
        engine_log = tmp_path / f"engine-{index}.log"
        env = _engine_environment(fake_bin, engine_log)
        env["CODEPROBE_CONTAINER_CONFIG"] = str(
            run_dir / f"container-images-{index}.json"
        )
        env.update(image_env)

        result = subprocess.run(
            [
                str(venv_dir / "bin" / "codeprobe"),
                "bootstrap",
                "--engine",
                "docker",
                "--no-json",
            ],
            cwd=run_dir,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )

        output = result.stdout + result.stderr
        assert result.returncode != 0
        assert expected in output
        assert (
            not engine_log.exists()
            or engine_log.read_text(encoding="utf-8") == ""
        )


def _install_wheel(wheel: Path, venv_dir: Path) -> Path:
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
    venv_python = venv_dir / "bin" / "python"
    installed_site = subprocess.run(
        [
            str(venv_python),
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dependency_site = Path(sysconfig.get_paths()["purelib"])
    (Path(installed_site) / "codeprobe-test-dependencies.pth").write_text(f"{dependency_site}\n", encoding="utf-8")
    subprocess.run(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-index",
            str(wheel),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    _install_source_guard(Path(installed_site), dependency_site)
    return venv_dir


def _install_source_guard(installed_site: Path, dependency_site: Path) -> None:
    guard = f"""import os
import sys

SOURCE_ROOT = os.path.realpath({str(REPO_ROOT)!r})
DEPENDENCY_SITE = os.path.realpath({str(dependency_site)!r})

def reject_source_reads(event, args):
    if event != "open" or not args or not isinstance(args[0], (str, bytes)):
        return
    candidate = os.path.realpath(os.fsdecode(args[0]))
    try:
        inside_source = os.path.commonpath((SOURCE_ROOT, candidate)) == SOURCE_ROOT
        inside_dependencies = os.path.commonpath((DEPENDENCY_SITE, candidate)) == DEPENDENCY_SITE
    except ValueError:
        inside_source = False
        inside_dependencies = False
    if inside_source and not inside_dependencies:
        raise RuntimeError(f"installed-wheel command read source checkout: {{candidate}}")

sys.addaudithook(reject_source_reads)
"""
    (installed_site / "codeprobe_test_source_guard.py").write_text(guard, encoding="utf-8")
    (installed_site / "zz-codeprobe-source-guard.pth").write_text(
        "import codeprobe_test_source_guard\n", encoding="utf-8"
    )


def _engine_environment(fake_bin: Path, engine_log: Path) -> dict[str, str]:
    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "CODEPROBE_FAKE_ENGINE_LOG": str(engine_log),
    }
    env.pop("PYTHONPATH", None)
    return env


def _installed_package_path(venv_python: Path, run_dir: Path, env: dict[str, str]) -> str:
    return subprocess.run(
        [
            str(venv_python),
            "-c",
            "import pathlib, codeprobe; print(pathlib.Path(codeprobe.__file__).resolve())",
        ],
        cwd=run_dir,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _run_installed_bootstrap(
    venv_dir: Path, run_dir: Path, config_path: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(venv_dir / "bin" / "codeprobe"),
            "bootstrap",
            "--engine",
            "docker",
            "--agent-image",
            _AGENT_REF,
            "--scoring-image",
            _SCORING_REF,
            "--no-json",
        ],
        cwd=run_dir,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _build_wheel(tmp_path: Path) -> Path:
    wheel_dir = tmp_path / "wheel"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--outdir",
            str(wheel_dir),
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = tuple(wheel_dir.glob("codeprobe-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def _write_fake_engine(path: Path) -> None:
    script = f"""#!/usr/bin/env python3
import json
import os
import pathlib
import sys

args = sys.argv[1:]
log = pathlib.Path(os.environ["CODEPROBE_FAKE_ENGINE_LOG"])
with log.open("a", encoding="utf-8") as stream:
    stream.write(" ".join(args) + "\\n")
if args[:2] == ["image", "pull"]:
    raise SystemExit(0)
if args[:2] != ["image", "inspect"] or len(args) != 3:
    raise SystemExit(2)
reference = args[2]
if "codeprobe-agent" in reference:
    local_id = "{_AGENT_LOCAL_ID}"
    digest = "{_AGENT_DIGEST}"
elif "codeprobe-scoring" in reference:
    local_id = "{_SCORING_LOCAL_ID}"
    digest = "{_SCORING_DIGEST}"
else:
    raise SystemExit(3)
print(json.dumps([{{"Id": local_id, "RepoDigests": [f"image@{{digest}}"]}}]))
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
