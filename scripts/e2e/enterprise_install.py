"""Fresh-wheel setup helpers for the enterprise release journey."""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import venv
from pathlib import Path
from typing import Final

from scripts.e2e.enterprise_artifacts import EnterpriseHarnessError

_AGENT_LOCAL_ID: Final[str] = "sha256:" + "c" * 64
_SCORING_LOCAL_ID: Final[str] = "sha256:" + "d" * 64


def install_candidate_wheel(
    wheel: Path,
    venv_dir: Path,
    *,
    source_root: Path,
    source_read_log: Path,
) -> tuple[Path, Path]:
    """Install one wheel with dependencies, then forbid checkout reads."""
    if not wheel.is_file():
        raise EnterpriseHarnessError("candidate wheel does not exist")
    venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
    python = venv_dir / "bin" / "python"
    pip = venv_dir / "bin" / "pip"
    _checked([str(pip), "install", "--disable-pip-version-check", str(wheel)])
    site_packages = _site_packages(python)
    _install_source_guard(site_packages, source_root, source_read_log)
    return python, venv_dir / "bin" / "codeprobe"


def installed_version(python: Path) -> tuple[str, Path]:
    """Return distribution version and package path from the staged venv."""
    script = (
        "import importlib.metadata, json, pathlib, codeprobe;"
        "print(json.dumps({'version': importlib.metadata.version('codeprobe'),"
        "'path': str(pathlib.Path(codeprobe.__file__).resolve())}))"
    )
    result = _checked([str(python), "-c", script])
    try:
        payload = json.loads(result.stdout)
        version = payload["version"]
        package_path = Path(payload["path"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise EnterpriseHarnessError("installed wheel identity is malformed") from exc
    if not isinstance(version, str):
        raise EnterpriseHarnessError("installed wheel version is malformed")
    return version, package_path


def write_agent_probe_shim(
    bin_dir: Path,
    *,
    agent: str,
    agent_image: str,
) -> Path:
    """Expose the selected image's CLI to doctor without a host install."""
    if agent != "claude":
        raise EnterpriseHarnessError(
            "the published agent image currently supports the claude path"
        )
    path = bin_dir / agent
    script = (
        "#!/bin/sh\n"
        "set -eu\n"
        "exec docker run --rm --network=none "
        f"{shlex.quote(agent_image)} claude \"$@\"\n"
    )
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def write_fake_private_registry_engine(
    bin_dir: Path,
    log_path: Path,
) -> Path:
    """Create a no-network engine fixture with deterministic image identity."""
    path = bin_dir / "docker"
    script = f"""#!/usr/bin/env python3
import json
import os
import pathlib
import sys

args = sys.argv[1:]
log = pathlib.Path(os.environ["CODEPROBE_PRIVATE_ENGINE_LOG"])
with log.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({{
        "args": args,
        "proxy": bool(os.environ.get("HTTPS_PROXY")),
        "private_ca": bool(os.environ.get("SSL_CERT_FILE")),
    }}) + "\\n")
if args[:2] == ["image", "pull"]:
    raise SystemExit(0)
if args[:2] != ["image", "inspect"] or len(args) != 3:
    raise SystemExit(2)
reference = args[2]
if "codeprobe-agent" in reference:
    local_id = "{_AGENT_LOCAL_ID}"
elif "codeprobe-scoring" in reference:
    local_id = "{_SCORING_LOCAL_ID}"
else:
    raise SystemExit(3)
digest = reference.rsplit("@", 1)[-1]
print(json.dumps([{{"Id": local_id, "RepoDigests": [f"image@{{digest}}"]}}]))
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    log_path.write_text("", encoding="utf-8")
    return path


def _site_packages(python: Path) -> Path:
    result = _checked(
        [
            str(python),
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ]
    )
    return Path(result.stdout.strip())


def _install_source_guard(
    site_packages: Path,
    source_root: Path,
    source_read_log: Path,
) -> None:
    guard = f"""import os
import pathlib
import sys

SOURCE_ROOT = os.path.realpath({str(source_root)!r})
READ_LOG = pathlib.Path({str(source_read_log)!r})

def reject_source_reads(event, args):
    if event != "open" or not args or not isinstance(args[0], (str, bytes)):
        return
    candidate = os.path.realpath(os.fsdecode(args[0]))
    try:
        inside = os.path.commonpath((SOURCE_ROOT, candidate)) == SOURCE_ROOT
    except ValueError:
        inside = False
    if inside:
        with READ_LOG.open("a", encoding="utf-8") as stream:
            stream.write(candidate + "\\n")
        raise RuntimeError("installed-wheel command read the source checkout")

sys.addaudithook(reject_source_reads)
"""
    source_read_log.write_text("", encoding="utf-8")
    (site_packages / "codeprobe_enterprise_source_guard.py").write_text(
        guard,
        encoding="utf-8",
    )
    (site_packages / "zz-codeprobe-enterprise-source-guard.pth").write_text(
        "import codeprobe_enterprise_source_guard\n",
        encoding="utf-8",
    )


def _checked(argv: list[str]) -> subprocess.CompletedProcess[str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name != "CODEPROBE_RELEASE_AGENT_CREDENTIAL"
    }
    try:
        return subprocess.run(
            argv,
            check=True,
            capture_output=True,
            env=environment,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EnterpriseHarnessError("fresh-wheel setup command failed") from exc


__all__ = [
    "install_candidate_wheel",
    "installed_version",
    "write_agent_probe_shim",
    "write_fake_private_registry_engine",
]
