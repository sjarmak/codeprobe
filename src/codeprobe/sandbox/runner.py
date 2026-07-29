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

from docker_image import reference as oci_reference  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


DEFAULT_IMAGE: Final[str] = "codeprobe-sandbox:sg-only"
DEFAULT_TIMEOUT_SECONDS: Final[float] = 60.0
_PACKAGE_NAME: Final[str] = "codeprobe"
_DEFAULT_AGENT_IMAGE_NAME: Final[str] = "codeprobe-agent"
_DEFAULT_SCORING_IMAGE_NAME: Final[str] = "codeprobe-scoring"

AGENT_IMAGE_ENV: Final[str] = "CODEPROBE_AGENT_IMAGE"
SCORING_IMAGE_ENV: Final[str] = "CODEPROBE_SCORING_IMAGE"
IMAGE_REGISTRY_ENV: Final[str] = "CODEPROBE_IMAGE_REGISTRY"
IMAGE_NAMESPACE_ENV: Final[str] = "CODEPROBE_IMAGE_NAMESPACE"
IMAGE_VERSION_ENV: Final[str] = "CODEPROBE_IMAGE_VERSION"

_IMAGE_TAG_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z"
)
_DIGEST_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"sha256:[a-f0-9]{64}\Z"
)
_CONTAINER_TMPFS: Final[str] = "/tmp:rw,nosuid,nodev,size=128m,mode=1777"

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
    """Return a stripped env value, rejecting empty or whitespace-containing refs."""
    value = os.environ.get(name)
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{name} must not be empty")
    if any(char.isspace() for char in stripped):
        raise ValueError(f"{name} must not contain whitespace")
    return stripped


def _validate_tag(name: str, tag: str) -> None:
    if tag == "latest":
        raise ValueError(f"{name} must not use the mutable latest tag")
    if _IMAGE_TAG_PATTERN.fullmatch(tag) is None:
        raise ValueError(f"{name} has an invalid image tag")


def _validate_image_reference(name: str, reference: str) -> str:
    if "://" in reference:
        raise ValueError(f"{name} must be an OCI image reference, not a URL")
    if "@" in reference:
        digest_candidate = reference.rsplit("@", 1)[1]
        if (
            digest_candidate.startswith("sha256:")
            and _DIGEST_PATTERN.fullmatch(digest_candidate) is None
        ):
            raise ValueError(f"{name} must use a sha256 digest when pinned")
    try:
        parsed = oci_reference.Reference.parse(reference)
    except oci_reference.InvalidReference as exc:
        raise ValueError(f"{name} has an invalid image reference") from exc

    tag = parsed.get("tag")
    digest = parsed.get("digest")
    if not isinstance(tag, str) and not isinstance(digest, str):
        raise ValueError(f"{name} must include an explicit tag or digest")
    if isinstance(tag, str):
        _validate_tag(name, tag)
    if isinstance(digest, str) and _DIGEST_PATTERN.fullmatch(digest) is None:
        raise ValueError(f"{name} must use a sha256 digest when pinned")
    return reference


def _composed_image_reference(image_name: str) -> str:
    version = _optional_env(IMAGE_VERSION_ENV) or _installed_version()
    _validate_tag(IMAGE_VERSION_ENV, version)
    registry_raw = _optional_env(IMAGE_REGISTRY_ENV)
    namespace_raw = _optional_env(IMAGE_NAMESPACE_ENV)
    registry = registry_raw.strip("/") if registry_raw is not None else ""
    namespace = namespace_raw.strip("/") if namespace_raw is not None else ""
    if registry_raw is not None and not registry:
        raise ValueError(f"{IMAGE_REGISTRY_ENV} has an invalid registry host")
    if registry_raw is not None and any(char.isupper() for char in registry):
        raise ValueError(f"{IMAGE_REGISTRY_ENV} has an invalid registry host")
    if namespace_raw is not None and not namespace:
        raise ValueError(f"{IMAGE_NAMESPACE_ENV} has an invalid repository path")
    if namespace_raw is not None and "//" in namespace:
        raise ValueError(f"{IMAGE_NAMESPACE_ENV} has an invalid repository path")
    repository_parts = [part for part in (registry, namespace, image_name) if part]
    return _validate_image_reference(
        "composed image reference", f"{'/'.join(repository_parts)}:{version}"
    )


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
    return _composed_image_reference(_DEFAULT_AGENT_IMAGE_NAME)


def scoring_image_reference() -> str:
    """Return the configured scoring container image reference."""
    return _image_reference(
        _DEFAULT_SCORING_IMAGE_NAME, exact_env=SCORING_IMAGE_ENV
    )


def scoring_image_build_tag() -> str:
    """Return a tag-shaped scoring image reference suitable for local builds."""
    return _composed_image_reference(_DEFAULT_SCORING_IMAGE_NAME)


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
    mode = "rw" if allow_writes else "ro"
    argv: list[str] = [
        engine,
        "run",
        "--rm",
        f"--network={network}",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
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
    if container_name is not None:
        argv += ["--name", container_name]

    if workdir is not None:
        argv += ["-w", workdir]

    if env:
        # Validate keys before constructing the -e KEY=VALUE argv so a
        # malformed key (empty, embedded '=', newline, or whitespace)
        # cannot silently produce a wrong env var inside the container.
        for key in env:
            if not key or "=" in key or "\n" in key or " " in key:
                raise ValueError(f"Invalid env var key: {key!r}")
        for key, value in env.items():
            argv += ["-e", f"{key}={value}"]

    for host_path, container_path in mounts.items():
        argv += ["-v", f"{host_path}:{container_path}:{mode}"]

    argv.append(image)

    if isinstance(cmd, str):
        # Wrap string commands in `sh -c` so shell features (pipes,
        # redirection, globbing) work as the caller expects.
        argv += ["sh", "-c", cmd]
    else:
        argv += list(cmd)

    return argv


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
    """Run ``cmd`` inside a sandbox container and capture its output.

    Parameters
    ----------
    cmd:
        Either a shell string (wrapped in ``sh -c``) or a list of argv tokens
        passed straight to the container entrypoint.
    mounts:
        Mapping of host path to container path. Host paths must already
        exist. When ``allow_writes`` is False (the default) every mount is
        bound ``:ro``.
    allow_writes:
        When True, mounts are bound ``:rw`` — required when the caller
        actually wants the sandbox to mutate the worktree.
    image:
        Container image tag. Defaults to ``codeprobe-sandbox:sg-only``
        (built from ``src/codeprobe/sandbox/Dockerfile.sg_only``).
    timeout:
        Wall-clock timeout in seconds. Exceeding it force-removes the
        container (the subprocess timeout only kills the engine CLI) and
        raises :class:`SandboxError`.
    workdir:
        Optional ``-w`` working directory inside the container.
    env:
        Optional environment variables forwarded with ``-e KEY=VAL``.
    network:
        Container network mode, emitted as ``--network=<value>``. Defaults
        to ``"none"`` so sandboxed commands have no egress; pass
        ``"bridge"`` only when the command genuinely needs the network.

    Returns
    -------
    :class:`SandboxResult`

    Raises
    ------
    SandboxError
        No container engine on PATH, subprocess timeout, or unexpected OS
        error while launching the engine.
    SandboxWriteDeniedError
        Command exited non-zero with stderr indicating a write to a
        read-only mount.
    """
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

    logger.debug("sandbox run: %s", argv)

    start = time.perf_counter()
    try:
        completed = subprocess.run(  # noqa: S603 — argv list, no shell=True
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # The timeout killed the engine client, not the container — force-
        # remove it so a hung mined script cannot outlive the run.
        _force_remove_container(engine, container_name)
        raise SandboxError(
            f"sandbox command timed out after {timeout:.1f}s: {argv!r}"
        ) from exc
    except FileNotFoundError as exc:
        # Defensive — _detect_engine already checked, but the engine binary
        # could be removed between that call and subprocess.run.
        raise SandboxError(f"sandbox engine not executable: {engine}") from exc

    duration_ms = int((time.perf_counter() - start) * 1000)

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    exit_code = completed.returncode

    if (
        exit_code != 0
        and not allow_writes
        and _looks_like_ro_write_failure(stderr)
    ):
        raise SandboxWriteDeniedError(
            f"sandbox blocked write to read-only mount (exit {exit_code}): "
            f"{stderr.strip()}"
        )

    return SandboxResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        duration_ms=duration_ms,
    )
