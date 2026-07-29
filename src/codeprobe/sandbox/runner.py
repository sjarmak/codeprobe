"""Container-based sandbox runner (INV4).

Executes a command inside a docker (preferred) or podman container with
host paths bind-mounted. Mounts are ``:ro`` by default, so the default
invocation cannot mutate the host worktree. The caller opts in to write
access by passing ``allow_writes=True``.

Design notes
------------

- Orchestration-only: this module is pure plumbing. It builds an argv,
  invokes the engine via :mod:`subprocess`, captures stdout/stderr, and
  translates known failure modes into exceptions. It makes no semantic
  judgments about the command being run.
- The engine is detected once per call via :func:`shutil.which`; docker
  wins when both are installed. Missing engine is a hard error — no
  silent fallback to host execution.
- Read-only mount violations are the one error class promoted to an
  exception so callers can distinguish "the sandbox prevented a write"
  from "the command exited non-zero". Every other non-zero exit is
  returned in :class:`SandboxResult` for the caller to inspect.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
import tomllib
import uuid
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Final

from codeprobe.sandbox.oci_references import (
    DIGEST_PATTERN,
    IMAGE_TAG_PATTERN,
    is_qualified_registry_host,
    validate_image_reference,
    validate_tag,
)

logger = logging.getLogger(__name__)


DEFAULT_IMAGE: Final[str] = "docker.io/library/codeprobe-sandbox:sg-only"
DEFAULT_TIMEOUT_SECONDS: Final[float] = 60.0
_PACKAGE_NAME: Final[str] = "codeprobe"
_DEFAULT_AGENT_IMAGE_NAME: Final[str] = "codeprobe-agent"
_DEFAULT_SCORING_IMAGE_NAME: Final[str] = "codeprobe-scoring"

AGENT_IMAGE_ENV: Final[str] = "CODEPROBE_AGENT_IMAGE"
SCORING_IMAGE_ENV: Final[str] = "CODEPROBE_SCORING_IMAGE"
IMAGE_REGISTRY_ENV: Final[str] = "CODEPROBE_IMAGE_REGISTRY"
IMAGE_NAMESPACE_ENV: Final[str] = "CODEPROBE_IMAGE_NAMESPACE"
IMAGE_VERSION_ENV: Final[str] = "CODEPROBE_IMAGE_VERSION"

_IMAGE_TAG_PATTERN: Final[re.Pattern[str]] = IMAGE_TAG_PATTERN
_DIGEST_PATTERN: Final[re.Pattern[str]] = DIGEST_PATTERN
_ENV_KEY_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_CONTAINER_TMPFS: Final[str] = "/tmp:rw,nosuid,nodev,size=128m,mode=1777"
_CONTAINER_CPUS: Final[str] = "2"
_CONTAINER_MEMORY: Final[str] = "4g"
_RUN_FLAGS_WITH_VALUE: Final[frozenset[str]] = frozenset(
    {
        "--name",
        "-w",
        "--workdir",
        "-e",
        "--env",
        "-v",
        "--volume",
        "--tmpfs",
        "--user",
    }
)
_DIAGNOSTIC_REDACT_VALUE_FLAGS: Final[frozenset[str]] = frozenset(
    {"-e", "--env", "-v", "--volume", "-w", "--workdir", "--tmpfs"}
)

# Lower-cased stderr fragments that indicate a write to a read-only mount.
# Kept explicit because the exact wording varies between docker, podman, and
# the underlying kernel, but these three substrings cover all observed cases.
_RO_WRITE_STDERR_PATTERNS: Final[tuple[str, ...]] = (
    "read-only file system",
    "read only file system",
    "permission denied",
)


@dataclass(frozen=True)
class SandboxResult:
    """Captured output from a completed sandbox run."""

    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int


class SandboxError(RuntimeError):
    """Base class for sandbox-runner failures (engine missing, timeout, etc.)."""


class SandboxWriteDeniedError(SandboxError):
    """Raised when a command tried to write to a read-only mount."""


_LEGACY_EXCEPTION_ALIASES = {
    "SandboxWriteDenied": "SandboxWriteDeniedError",
}


def _installed_version() -> str:
    """Return the installed codeprobe version used for default image tags."""
    try:
        return package_version(_PACKAGE_NAME)
    except PackageNotFoundError:
        pyproject = Path(__file__).resolve().parents[3] / "pyproject.toml"
        if pyproject.is_file():
            project = tomllib.loads(pyproject.read_text())
            version = project.get("project", {}).get("version")
            if isinstance(version, str) and version:
                return version
        raise RuntimeError(
            "Cannot resolve the installed codeprobe version; set "
            f"{IMAGE_VERSION_ENV} or an exact image override."
        ) from None


# Image used to execute mined test.sh / verifier scripts (codeprobe-f7rl.4).
# Built from ``src/codeprobe/sandbox/Dockerfile.scoring``. The default version
# tag tracks the installed codeprobe package version.
DEFAULT_IMAGE_VERSION: Final[str] = _installed_version()
DEFAULT_SCORING_IMAGE: Final[str] = f"{_DEFAULT_SCORING_IMAGE_NAME}:{DEFAULT_IMAGE_VERSION}"

# Image used to run the agent subprocess itself (codeprobe-f7rl.5). Built
# from ``src/codeprobe/sandbox/Dockerfile.agent``. The default version tag
# tracks the installed codeprobe package version.
DEFAULT_AGENT_IMAGE: Final[str] = f"{_DEFAULT_AGENT_IMAGE_NAME}:{DEFAULT_IMAGE_VERSION}"


def _optional_env(name: str) -> str | None:
    """Return an env value, rejecting empty or whitespace-containing refs."""
    value = os.environ.get(name)
    if value is None:
        return None
    if not value:
        raise ValueError(f"{name} must not be empty")
    if value != value.strip() or any(char.isspace() for char in value):
        raise ValueError(f"{name} must not contain whitespace")
    return value


def _validate_tag(name: str, tag: str) -> None:
    validate_tag(name, tag)


def _is_qualified_registry_host(host: str) -> bool:
    return is_qualified_registry_host(host)


def _validate_image_reference(name: str, reference: str) -> str:
    return validate_image_reference(name, reference)


def _composed_image_reference(image_name: str) -> str:
    version = _optional_env(IMAGE_VERSION_ENV) or _installed_version()
    _validate_tag(IMAGE_VERSION_ENV, version)
    registry_raw = _optional_env(IMAGE_REGISTRY_ENV)
    namespace_raw = _optional_env(IMAGE_NAMESPACE_ENV)
    if registry_raw is None or namespace_raw is None:
        raise ValueError(
            f"Set both {IMAGE_REGISTRY_ENV} and {IMAGE_NAMESPACE_ENV}, or set "
            "an exact per-image override."
        )
    registry = registry_raw
    namespace = namespace_raw
    return _compose_validated_image_reference(
        image_name=image_name, version=version, registry=registry, namespace=namespace
    )


def _compose_validated_image_reference(
    *, image_name: str, version: str, registry: str, namespace: str
) -> str:
    if not registry:
        raise ValueError(f"{IMAGE_REGISTRY_ENV} has an invalid registry host")
    if any(char.isupper() for char in registry):
        raise ValueError(f"{IMAGE_REGISTRY_ENV} has an invalid registry host")
    if "/" in registry or not _is_qualified_registry_host(registry):
        raise ValueError(f"{IMAGE_REGISTRY_ENV} has an invalid registry host")
    if not namespace:
        raise ValueError(f"{IMAGE_NAMESPACE_ENV} has an invalid repository path")
    if namespace.startswith("/") or namespace.endswith("/"):
        raise ValueError(f"{IMAGE_NAMESPACE_ENV} has an invalid repository path")
    if "//" in namespace:
        raise ValueError(f"{IMAGE_NAMESPACE_ENV} has an invalid repository path")
    repository_parts = [part for part in (registry, namespace, image_name) if part]
    return _validate_image_reference(
        "composed image reference", f"{'/'.join(repository_parts)}:{version}"
    )


def _local_image_build_tag(image_name: str) -> str:
    version = _optional_env(IMAGE_VERSION_ENV) or _installed_version()
    _validate_tag(IMAGE_VERSION_ENV, version)
    return f"{image_name}:{version}"


def _image_reference(image_name: str, *, exact_env: str) -> str:
    exact = _optional_env(exact_env)
    if exact is not None:
        return _validate_image_reference(exact_env, exact)
    return _composed_image_reference(image_name)


def agent_image_reference() -> str:
    """Return the configured agent container image reference."""
    return _image_reference(_DEFAULT_AGENT_IMAGE_NAME, exact_env=AGENT_IMAGE_ENV)


def agent_image_build_tag() -> str:
    """Return a tag-shaped agent image reference suitable for local builds."""
    return _local_image_build_tag(_DEFAULT_AGENT_IMAGE_NAME)


def scoring_image_reference() -> str:
    """Return the configured scoring container image reference."""
    return _image_reference(
        _DEFAULT_SCORING_IMAGE_NAME, exact_env=SCORING_IMAGE_ENV
    )


def scoring_image_build_tag() -> str:
    """Return a tag-shaped scoring image reference suitable for local builds."""
    return _local_image_build_tag(_DEFAULT_SCORING_IMAGE_NAME)


def __getattr__(name: str) -> object:
    """Legacy-alias shim — see :mod:`codeprobe.calibration.gate` for rationale."""
    new_name = _LEGACY_EXCEPTION_ALIASES.get(name)
    if new_name is not None:
        import warnings

        warnings.warn(
            f"{name} is deprecated; use {new_name}. "
            "The alias will be removed in v0.9.",
            DeprecationWarning,
            stacklevel=2,
        )
        return globals()[new_name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _detect_engine() -> str:
    """Return the path to docker or podman, preferring docker.

    Raises :class:`SandboxError` when neither is installed on PATH.
    """
    docker_path = shutil.which("docker")
    if docker_path:
        return docker_path
    podman_path = shutil.which("podman")
    if podman_path:
        return podman_path
    raise SandboxError(
        "No container engine found on PATH. Install docker or podman to use "
        "the codeprobe sandbox."
    )


def detect_engine() -> str | None:
    """Return the path to docker or podman, or ``None`` when neither exists.

    Non-raising wrapper around :func:`_detect_engine` for callers that
    treat "no engine" as a branch condition rather than an error.
    """
    try:
        return _detect_engine()
    except SandboxError:
        return None


def image_available(engine: str, image: str) -> bool:
    """Return True when *image* exists locally for *engine*.

    Uses ``<engine> image inspect`` (supported by both docker and podman).
    Any failure — nonzero exit, missing binary, timeout — reads as "image
    not available"; callers fall through to their no-container branch.
    """
    validate_image_reference("sandbox image", image)
    try:
        completed = subprocess.run(  # noqa: S603 — argv list, no shell=True
            [engine, "image", "inspect", image],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _build_run_command(
    engine: str,
    cmd: list[str] | str,
    mounts: dict[str, str],
    *,
    allow_writes: bool,
    image: str,
    workdir: str | None,
    env: dict[str, str] | None,
    network: str = "none",
    container_name: str | None = None,
) -> list[str]:
    """Build the argv for ``<engine> run ...``.

    Exposed as a module-private helper so unit tests can assert the flags
    without spawning a container. ``network`` defaults to ``"none"`` — the
    mined-script posture; callers that need egress (the agent talks to the
    model API) pass ``"bridge"`` explicitly. ``container_name`` emits
    ``--name`` so a client-side timeout can ``<engine> rm -f`` the
    container instead of orphaning it.
    """
    validate_image_reference("sandbox image", image)
    argv = _run_base_args(engine, network)
    if container_name is not None:
        argv += ["--name", container_name]
    if workdir is not None:
        argv += ["-w", workdir]
    argv += _env_args(env)
    mode = "rw" if allow_writes else "ro"
    argv += _mount_args(mounts, mode)
    argv.append(image)
    argv += _command_args(cmd)
    return argv


def _run_base_args(engine: str, network: str) -> list[str]:
    argv = [
        engine,
        "run",
        "--rm",
        "--pull=never",
        f"--network={network}",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--cpus={_CONTAINER_CPUS}",
        f"--memory={_CONTAINER_MEMORY}",
        f"--memory-swap={_CONTAINER_MEMORY}",
        "--pids-limit=256",
        "--read-only",
        "--tmpfs",
        _CONTAINER_TMPFS,
        "-e",
        "HOME=/tmp",
        "-e",
        "TMPDIR=/tmp",
    ]
    if hasattr(os, "getuid") and hasattr(os, "getgid"):
        argv += ["--user", f"{os.getuid()}:{os.getgid()}"]
    return argv


def _env_args(env: dict[str, str] | None) -> list[str]:
    if not env:
        return []
    args: list[str] = []
    for key, value in env.items():
        if _ENV_KEY_RE.fullmatch(key) is None:
            raise ValueError(f"Invalid env var key: {key!r}")
        args += ["-e", f"{key}={value}"]
    return args


def _mount_args(mounts: dict[str, str], mode: str) -> list[str]:
    args: list[str] = []
    for host_path, container_path in mounts.items():
        args += ["-v", f"{host_path}:{container_path}:{mode}"]
    return args


def _command_args(cmd: list[str] | str) -> list[str]:
    if isinstance(cmd, str):
        return ["sh", "-c", cmd]
    return list(cmd)


def _diagnostic_argv(argv: list[str]) -> list[str]:
    safe: list[str] = []
    redact_next = ""
    command_start = _command_start_index(argv)
    for index, token in enumerate(argv):
        if index >= command_start:
            safe.append("<redacted-command>")
            break
        if index == 0:
            safe.append(os.path.basename(token) if token.startswith("/") else token)
            continue
        if redact_next:
            safe.append(_redacted_argument(redact_next, token))
            redact_next = ""
            continue
        safe.append(token)
        if token in _DIAGNOSTIC_REDACT_VALUE_FLAGS:
            redact_next = token
    return safe


def _command_start_index(argv: list[str]) -> int:
    index = 2
    while index < len(argv):
        token = argv[index]
        if token in _RUN_FLAGS_WITH_VALUE:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return index + 1
    return len(argv)


def _redacted_argument(flag: str, token: str) -> str:
    if flag in {"-e", "--env"}:
        key = token.split("=", 1)[0]
        return f"{key}=<redacted>" if "=" in token else key
    if flag in {"-v", "--volume"}:
        parts = token.rsplit(":", 2)
        mode = parts[2] if len(parts) == 3 else "<mode>"
        return f"<host-path>:<container-path>:{mode}"
    return "<path>"


def _looks_like_ro_write_failure(stderr: str) -> bool:
    """Return True when stderr looks like a write-to-ro-mount error."""
    haystack = stderr.lower()
    return any(needle in haystack for needle in _RO_WRITE_STDERR_PATTERNS)


# Removal after a client-side kill can race the daemon: killing the engine
# CLI mid-``run`` may leave the create request in flight, so an immediate
# ``rm -f`` finds nothing and the container lands afterwards (Created,
# never started) — observed live; it pins the image until force-removed.
_REMOVE_ATTEMPTS: Final[int] = 3
_REMOVE_RETRY_DELAY_SECONDS: Final[float] = 1.0


def _container_exists(engine: str, name: str) -> bool:
    """Return True when a container named *name* exists in any state."""
    try:
        completed = subprocess.run(  # noqa: S603 — argv list, no shell=True
            [engine, "container", "inspect", name],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _force_remove_container(engine: str, name: str) -> None:
    """Best-effort ``<engine> rm -f <name>`` after a client-side timeout.

    ``subprocess.run(timeout=...)`` kills the engine CLI, not the
    container; without this a hung sandbox command (mined third-party
    test.sh, untrusted by definition) leaves its container running
    indefinitely. Removal is retried a bounded number of times because the
    kill can race the daemon's create (see ``_REMOVE_ATTEMPTS`` above).
    Failures are logged, never raised — the caller is already translating
    the timeout into :class:`SandboxError`.
    """
    for _ in range(_REMOVE_ATTEMPTS):
        try:
            subprocess.run(  # noqa: S603 — argv list, no shell=True
                [engine, "rm", "-f", name],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            logger.warning(
                "failed to remove timed-out sandbox container %s", name
            )
            return
        time.sleep(_REMOVE_RETRY_DELAY_SECONDS)
        if not _container_exists(engine, name):
            return
    logger.warning(
        "sandbox container %s still present after %d removal attempts",
        name,
        _REMOVE_ATTEMPTS,
    )


def run_in_sandbox(
    cmd: list[str] | str,
    mounts: dict[str, str],
    *,
    allow_writes: bool = False,
    image: str = DEFAULT_IMAGE,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    workdir: str | None = None,
    env: dict[str, str] | None = None,
    network: str = "none",
) -> SandboxResult:
    """Run ``cmd`` inside a sandbox container and capture its output."""
    engine = _detect_engine()
    container_name = f"codeprobe-sb-{uuid.uuid4().hex}"
    argv = _build_run_command(
        engine,
        cmd,
        mounts,
        allow_writes=allow_writes,
        image=image,
        workdir=workdir,
        env=env,
        network=network,
        container_name=container_name,
    )
    start = time.perf_counter()
    logger.debug("sandbox run: %s", _diagnostic_argv(argv))
    completed = _run_container_command(argv, engine, container_name, timeout)
    return _sandbox_result(completed, start, allow_writes)


def _run_container_command(
    argv: list[str], engine: str, container_name: str, timeout: float
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 — argv list, no shell=True
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _force_remove_container(engine, container_name)
        raise SandboxError(
            "sandbox command timed out after "
            f"{timeout:.1f}s: {_diagnostic_argv(argv)!r}"
        ) from exc
    except OSError as exc:
        raise SandboxError("sandbox engine failed to launch") from exc


def _sandbox_result(
    completed: subprocess.CompletedProcess[str], start: float, allow_writes: bool
) -> SandboxResult:
    duration_ms = int((time.perf_counter() - start) * 1000)
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    exit_code = completed.returncode
    if _should_raise_write_denied(exit_code, allow_writes, stderr):
        raise SandboxWriteDeniedError(
            f"sandbox blocked write to read-only mount (exit {exit_code})"
        )

    return SandboxResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        duration_ms=duration_ms,
    )


def _should_raise_write_denied(
    exit_code: int, allow_writes: bool, stderr: str
) -> bool:
    return (
        exit_code != 0
        and not allow_writes
        and _looks_like_ro_write_failure(stderr)
    )
