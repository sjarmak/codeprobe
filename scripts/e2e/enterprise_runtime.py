"""Subprocess and journey-step runtime for enterprise acceptance."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from scripts.e2e.enterprise_artifacts import (
    EnterpriseHarnessError,
    parse_envelope,
)


class JourneyRuntime:
    """Run installed CodeProbe commands and retain only captured output."""

    def __init__(
        self,
        codeprobe: Path,
        *,
        env: Mapping[str, str],
        timeout: int,
    ) -> None:
        self._codeprobe = codeprobe
        self._env = dict(env)
        self._timeout = timeout
        self.outputs: list[str] = []

    @property
    def environment(self) -> dict[str, str]:
        """Return an immutable-by-copy view of the base environment."""
        return dict(self._env)

    def codeprobe(
        self,
        step: str,
        args: list[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        result = self._run(
            [str(self._codeprobe), *args, "--json"],
            cwd=cwd,
            env=env,
        )
        if result.returncode != 0:
            raise EnterpriseHarnessError(f"enterprise journey step {step} failed")
        return parse_envelope(result.stdout, step)

    def expect_codeprobe_error(
        self,
        args: list[str],
        *,
        cwd: Path,
        expected_code: str,
    ) -> None:
        result = self._run(
            [str(self._codeprobe), *args, "--json"],
            cwd=cwd,
        )
        envelope = _error_envelope(result.stdout)
        error = envelope.get("error")
        if (
            result.returncode == 0
            or not isinstance(error, dict)
            or error.get("code") != expected_code
        ):
            raise EnterpriseHarnessError(
                "structured error contract did not return the expected code"
            )

    def external(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        expected_returncode: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        result = self._run(argv, cwd=cwd, env=env)
        if result.returncode != expected_returncode:
            raise EnterpriseHarnessError("enterprise fixture command failed")
        return result

    def _run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        effective_env = {**self._env, **(dict(env) if env is not None else {})}
        try:
            result = subprocess.run(
                argv,
                cwd=cwd,
                env=effective_env,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise EnterpriseHarnessError("enterprise journey command failed") from exc
        self.outputs.extend((result.stdout, result.stderr))
        return result


def envelope_data(envelope: Mapping[str, Any], step: str) -> dict[str, Any]:
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise EnterpriseHarnessError(f"{step} envelope data is malformed")
    return data


def _error_envelope(stdout: str) -> dict[str, Any]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise EnterpriseHarnessError("structured error envelope is missing")
    try:
        raw = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise EnterpriseHarnessError("structured error envelope is missing") from exc
    if (
        not isinstance(raw, dict)
        or raw.get("record_type") != "envelope"
        or raw.get("ok") is not False
    ):
        raise EnterpriseHarnessError("structured error envelope is missing")
    return raw


def base_environment(
    *,
    home: Path,
    shim_bin: Path,
    config_path: Path,
    credential_env: str,
    credential_value: str,
    agent_image: str,
    scoring_image: str,
) -> dict[str, str]:
    """Build an isolated runtime environment with one credential mapping."""
    if not credential_env.isidentifier():
        raise EnterpriseHarnessError("credential environment name is invalid")
    inherited = {
        name: value
        for name, value in os.environ.items()
        if name != "CODEPROBE_RELEASE_AGENT_CREDENTIAL"
    }
    return {
        **inherited,
        "HOME": str(home),
        "PATH": f"{shim_bin}{os.pathsep}{os.environ['PATH']}",
        "CODEPROBE_CONTAINER_CONFIG": str(config_path),
        "CODEPROBE_AGENT_IMAGE": agent_image,
        "CODEPROBE_SCORING_IMAGE": scoring_image,
        "CODEPROBE_TENANT": "release-enterprise-journey",
        credential_env: credential_value,
    }


__all__ = ["JourneyRuntime", "base_environment", "envelope_data"]
