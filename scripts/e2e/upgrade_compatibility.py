#!/usr/bin/env python3
"""Exercise the published prior wheel's artifacts after a candidate upgrade."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
import zipfile
from collections.abc import Mapping
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.e2e.make_fixture_repo import make_python_fixture_repo  # noqa: E402

PRIOR_RELEASE_VERSION: Final[str] = "0.11.0"
PRIOR_RELEASE_SHA256: Final[str] = (
    "a7797a1f4be4a6b4bd9ce73cb4ac868d8e26e2d4a23a3ecda040ee19105bfbf5"
)
_MAX_WHEEL_METADATA_BYTES: Final[int] = 1024 * 1024
_INSTALL_ENV_NAMES: Final[frozenset[str]] = frozenset(
    {
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "PATH",
        "PIP_CERT",
        "PIP_EXTRA_INDEX_URL",
        "PIP_INDEX_URL",
        "PIP_TRUSTED_HOST",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_FILE",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)


class UpgradeCompatibilityError(RuntimeError):
    """Raised when the prior-to-candidate contract does not hold."""


def validate_wheel(path: Path, *, expected_sha256: str | None = None) -> None:
    """Require one readable wheel and, when supplied, its exact digest."""
    if not path.is_file() or path.suffix != ".whl":
        raise UpgradeCompatibilityError("upgrade wheel cannot be read")
    if expected_sha256 is not None and _sha256(path) != expected_sha256:
        raise UpgradeCompatibilityError("prior release wheel digest does not match")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise UpgradeCompatibilityError("upgrade wheel cannot be read") from exc
    return digest.hexdigest()


def _wheel_version(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            metadata_entries = [
                info
                for info in archive.infolist()
                if info.filename.endswith(".dist-info/METADATA")
            ]
            if (
                len(metadata_entries) != 1
                or metadata_entries[0].file_size > _MAX_WHEEL_METADATA_BYTES
            ):
                raise UpgradeCompatibilityError("candidate wheel metadata is invalid")
            metadata = BytesParser().parsebytes(archive.read(metadata_entries[0]))
    except (
        OSError,
        KeyError,
        NotImplementedError,
        RuntimeError,
        zipfile.BadZipFile,
    ) as exc:
        raise UpgradeCompatibilityError("candidate wheel metadata is invalid") from exc
    version = metadata.get("Version")
    if not version:
        raise UpgradeCompatibilityError("candidate wheel version is missing")
    return version


def runtime_environment(home: Path) -> dict[str, str]:
    """Return the minimal environment exposed to installed CodeProbe code."""
    return {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", os.defpath),
        "CODEPROBE_TENANT": "release-upgrade-compatibility",
    }


def _install_environment(home: Path) -> dict[str, str]:
    """Return only package-index and transport settings required by pip."""
    return {
        **{
            name: value
            for name, value in os.environ.items()
            if name in _INSTALL_ENV_NAMES
        },
        "HOME": str(home),
    }


def _run(
    argv: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    step: str,
    expected_returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpgradeCompatibilityError(f"{step} failed") from exc
    if result.returncode != expected_returncode:
        raise UpgradeCompatibilityError(f"{step} failed")
    return result


def successful_envelope(
    result: subprocess.CompletedProcess[str],
    step: str,
) -> dict[str, Any]:
    """Parse the final JSON envelope after any legacy human-readable output."""
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    try:
        raw = json.loads(lines[-1]) if lines else None
    except (IndexError, json.JSONDecodeError) as exc:
        raise UpgradeCompatibilityError(f"{step} response is invalid") from exc
    if not isinstance(raw, dict) or raw.get("ok") is not True:
        raise UpgradeCompatibilityError(f"{step} did not accept prior artifacts")
    return raw


def validate_interpretation(payload: Mapping[str, object]) -> None:
    """Require exactly one result with fully measured cost telemetry."""
    data = payload.get("data")
    report = data.get("report") if isinstance(data, dict) else None
    summaries = report.get("summaries") if isinstance(report, dict) else None
    if (
        not isinstance(summaries, list)
        or len(summaries) != 1
        or not isinstance(summaries[0], dict)
        or summaries[0].get("cost_coverage") != 1.0
    ):
        raise UpgradeCompatibilityError(
            "candidate result interpretation is incomplete"
        )


def _error_envelope_code(result: subprocess.CompletedProcess[str]) -> str | None:
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    try:
        payload = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError as exc:
        raise UpgradeCompatibilityError(
            "legacy snapshot refusal response is invalid"
        ) from exc
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


def _prior_artifacts(
    python: Path,
    codeprobe: Path,
    fixture: Path,
    snapshot: Path,
    env: Mapping[str, str],
) -> None:
    _run(
        [str(codeprobe), "experiment", "init", str(fixture), "--non-interactive",
         "--name", "upgrade"],
        cwd=fixture,
        env=env,
        step="prior experiment initialization",
    )
    successful_envelope(
        _run(
            [str(codeprobe), "mine", str(fixture), "--no-interactive", "--goal",
             "quality", "--count", "1", "--no-llm", "--json"],
            cwd=fixture,
            env=env,
            step="prior task mining",
        ),
        "prior mine",
    )
    experiment = fixture / ".codeprobe"
    _run(
        [str(codeprobe), "experiment", "add-config", str(experiment),
         "--label", "baseline", "--agent", "claude"],
        cwd=fixture,
        env=env,
        step="prior configuration registration",
    )
    _run(
        [str(python), str(REPO_ROOT / "scripts/e2e/prior_release_artifacts.py"),
         str(experiment)],
        cwd=fixture,
        env=env,
        step="prior result creation",
    )
    successful_envelope(
        _run(
            [str(codeprobe), "snapshot", "create", str(experiment), "--out",
             str(snapshot), "--redact", "hashes-only", "--json"],
            cwd=fixture,
            env=env,
            step="prior snapshot creation",
        ),
        "prior snapshot create",
    )


def _candidate_reads(
    codeprobe: Path,
    fixture: Path,
    snapshot: Path,
    env: Mapping[str, str],
) -> None:
    successful_envelope(
        _run(
            [str(codeprobe), "validate", str(fixture / ".codeprobe/tasks"), "--json"],
            cwd=fixture,
            env=env,
            step="candidate task validation",
        ),
        "candidate task validation",
    )
    interpreted = successful_envelope(
        _run(
            [str(codeprobe), "interpret", str(fixture), "--format", "json", "--json"],
            cwd=fixture,
            env=env,
            step="candidate result interpretation",
        ),
        "candidate result interpretation",
    )
    validate_interpretation(interpreted)
    refused = _run(
        [str(codeprobe), "snapshot", "verify", str(snapshot), "--json"],
        cwd=fixture,
        env=env,
        step="candidate legacy snapshot refusal",
        expected_returncode=2,
    )
    if _error_envelope_code(refused) != "SNAPSHOT_UNSAFE_LEGACY_FORMAT":
        raise UpgradeCompatibilityError("legacy snapshot was not refused prescriptively")


def run_upgrade(prior_wheel: Path, candidate_wheel: Path, workdir: Path) -> None:
    """Generate artifacts with the prior wheel, upgrade, then read or refuse."""
    validate_wheel(prior_wheel, expected_sha256=PRIOR_RELEASE_SHA256)
    validate_wheel(candidate_wheel)
    candidate_version = _wheel_version(candidate_wheel)
    venv_dir = workdir / "venv"
    venv.EnvBuilder(with_pip=True).create(venv_dir)
    python = venv_dir / "bin" / "python"
    pip = venv_dir / "bin" / "pip"
    codeprobe = venv_dir / "bin" / "codeprobe"
    home = workdir / "home"
    home.mkdir()
    install_env = _install_environment(home)
    runtime_env = runtime_environment(home)
    _run([str(pip), "install", "--disable-pip-version-check", str(prior_wheel)],
         cwd=workdir, env=install_env, step="prior release installation")
    version = _run(
        [str(codeprobe), "--version"],
        cwd=workdir,
        env=runtime_env,
        step="prior version check",
    ).stdout
    if version.strip() != f"codeprobe, version {PRIOR_RELEASE_VERSION}":
        raise UpgradeCompatibilityError("prior release version does not match")
    fixture = make_python_fixture_repo(workdir / "fixture", feature_count=1)
    snapshot = workdir / "legacy-snapshot"
    _prior_artifacts(python, codeprobe, fixture, snapshot, runtime_env)
    _run(
        [str(pip), "install", "--disable-pip-version-check", "--upgrade",
         str(candidate_wheel)],
        cwd=workdir,
        env=install_env,
        step="candidate installation",
    )
    upgraded = _run(
        [str(codeprobe), "--version"],
        cwd=workdir,
        env=runtime_env,
        step="candidate version check",
    ).stdout
    if upgraded.strip() != f"codeprobe, version {candidate_version}":
        raise UpgradeCompatibilityError("candidate upgrade version does not match")
    _candidate_reads(
        codeprobe,
        fixture,
        snapshot,
        runtime_env,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prior-wheel", required=True, type=Path)
    parser.add_argument("--candidate-wheel", required=True, type=Path)
    parser.add_argument("--keep-workdir", action="store_true")
    args = parser.parse_args(argv)
    workdir = Path(tempfile.mkdtemp(prefix="codeprobe-upgrade-"))
    try:
        run_upgrade(args.prior_wheel.resolve(), args.candidate_wheel.resolve(), workdir)
    except (
        UpgradeCompatibilityError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"upgrade compatibility gate failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if args.keep_workdir:
            print(f"upgrade workdir retained at {workdir}", file=sys.stderr)
        else:
            shutil.rmtree(workdir)
    print("prior-release-to-candidate upgrade compatibility passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PRIOR_RELEASE_SHA256",
    "PRIOR_RELEASE_VERSION",
    "UpgradeCompatibilityError",
    "run_upgrade",
    "runtime_environment",
    "successful_envelope",
    "validate_interpretation",
    "validate_wheel",
]
