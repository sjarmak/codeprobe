"""Pull or import containment images and persist immutable local identities."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from codeprobe.sandbox.image_config import (
    EngineName,
    PreparedImage,
    PreparedImages,
    write_prepared_images,
)
from codeprobe.sandbox.oci_references import (
    DIGEST_PATTERN,
    LOCAL_IMAGE_ID_PATTERN,
    validate_image_reference,
)

COMMAND_TIMEOUT_SECONDS: Final[float] = 600.0
INSPECT_TIMEOUT_SECONDS: Final[float] = 30.0
_MAX_COMMAND_OUTPUT_BYTES: Final[int] = 1_048_576
CommandRunner = Callable[[Sequence[str], float], str]
ExecutableResolver = Callable[[str], str | None]


class ImageBootstrapError(RuntimeError):
    """A fail-closed image preparation failure safe to show to an operator."""


@dataclass(frozen=True)
class BootstrapResult:
    prepared: PreparedImages
    config_path: Path

    @property
    def engine(self) -> EngineName:
        return self.prepared.engine

    @property
    def agent(self) -> PreparedImage:
        return self.prepared.agent

    @property
    def scoring(self) -> PreparedImage:
        return self.prepared.scoring


@dataclass(frozen=True)
class _ImageRequest:
    label: str
    source_reference: str
    verified_reference: str
    digest: str
    archive: Path | None


def prepare_images(
    *,
    engine: str | None,
    agent_reference: str,
    scoring_reference: str,
    agent_digest: str | None = None,
    scoring_digest: str | None = None,
    agent_archive: Path | None = None,
    scoring_archive: Path | None = None,
    config_path: Path | None = None,
    runner: CommandRunner | None = None,
    which: ExecutableResolver = shutil.which,
) -> BootstrapResult:
    """Prepare both runtime images and persist their immutable local IDs."""
    _validate_archive_pair(agent_archive, scoring_archive)
    engine_name, engine_path = _select_engine(engine, which)
    requests = (
        _request("agent", agent_reference, agent_digest, agent_archive),
        _request("scoring", scoring_reference, scoring_digest, scoring_archive),
    )
    command_runner = runner or _run_text_command
    if agent_archive is None:
        images = tuple(_prepare_online(request, engine_path, command_runner) for request in requests)
    else:
        skopeo_path = _require_tool("skopeo", which)
        images = tuple(
            _prepare_offline(request, engine_name, engine_path, skopeo_path, command_runner) for request in requests
        )
    prepared = PreparedImages(engine_name, images[0], images[1])
    written = write_prepared_images(prepared, config_path)
    return BootstrapResult(prepared=prepared, config_path=written)


def _request(label: str, reference: str, digest_override: str | None, archive: Path | None) -> _ImageRequest:
    try:
        validate_image_reference(f"{label} image", reference)
    except ValueError as exc:
        raise ImageBootstrapError(str(exc)) from exc
    reference_digest = reference.rsplit("@", 1)[1] if "@" in reference else None
    digest = _resolve_digest(label, reference_digest, digest_override)
    verified = reference if reference_digest is not None else f"{reference}@{digest}"
    try:
        validate_image_reference(f"{label} verified image", verified)
    except ValueError as exc:
        raise ImageBootstrapError(str(exc)) from exc
    return _ImageRequest(label, reference, verified, digest, archive)


def _resolve_digest(label: str, reference_digest: str | None, digest_override: str | None) -> str:
    if digest_override is not None and DIGEST_PATTERN.fullmatch(digest_override) is None:
        raise ImageBootstrapError(f"{label} digest must be a sha256 digest")
    if reference_digest is not None and DIGEST_PATTERN.fullmatch(reference_digest) is None:
        raise ImageBootstrapError(f"{label} image must use a sha256 digest")
    if reference_digest is not None and digest_override not in (None, reference_digest):
        raise ImageBootstrapError(f"{label} digest does not match the digest-pinned image reference")
    digest = reference_digest or digest_override
    if digest is None:
        raise ImageBootstrapError(f"{label} image requires an expected sha256 digest")
    return digest


def _select_engine(requested: str | None, which: ExecutableResolver) -> tuple[EngineName, str]:
    if requested is not None:
        if requested not in ("docker", "podman"):
            raise ImageBootstrapError("engine must be docker or podman")
        path = which(requested)
        if path is None:
            raise ImageBootstrapError(f"{requested} was not found on PATH")
        return cast(EngineName, requested), path
    for name in ("docker", "podman"):
        path = which(name)
        if path is not None:
            return name, path
    raise ImageBootstrapError("No container engine found; install docker or podman")


def _require_tool(name: str, which: ExecutableResolver) -> str:
    path = which(name)
    if path is None:
        raise ImageBootstrapError(f"{name} was not found on PATH; OCI archive loading requires {name}")
    return path


def _prepare_online(request: _ImageRequest, engine_path: str, runner: CommandRunner) -> PreparedImage:
    runner(
        [engine_path, "image", "pull", request.verified_reference],
        COMMAND_TIMEOUT_SECONDS,
    )
    return _inspect_prepared_image(
        request,
        engine_path,
        request.verified_reference,
        runner,
    )


def _prepare_offline(
    request: _ImageRequest,
    engine: EngineName,
    engine_path: str,
    skopeo_path: str,
    runner: CommandRunner,
) -> PreparedImage:
    destination = _destination_reference(request)
    transport = "docker-daemon" if engine == "docker" else "containers-storage"
    transported_destination = f"{transport}:{destination}"
    with _private_archive_snapshot(request) as (archive, digest_path):
        source = f"oci-archive:{archive}"
        observed = runner(
            [skopeo_path, "inspect", "--format", "{{.Digest}}", source],
            INSPECT_TIMEOUT_SECONDS,
        ).strip()
        if observed != request.digest:
            raise ImageBootstrapError(f"{request.label} archive digest mismatch")
        copy_options = ["--format", "v2s2"] if engine == "docker" else ["--preserve-digests"]
        runner(
            [
                skopeo_path,
                "copy",
                *copy_options,
                "--digestfile",
                str(digest_path),
                source,
                transported_destination,
            ],
            COMMAND_TIMEOUT_SECONDS,
        )
        copied_digest = _read_copied_digest(request.label, digest_path)
        raw_manifest = runner(
            [skopeo_path, "inspect", "--raw", transported_destination],
            INSPECT_TIMEOUT_SECONDS,
        )
        config_digest = _verified_config_digest(request.label, raw_manifest, copied_digest)
        return _inspect_offline_image(
            request,
            engine_path,
            destination,
            copied_digest,
            config_digest,
            runner,
        )


@contextmanager
def _private_archive_snapshot(request: _ImageRequest) -> Iterator[tuple[Path, Path]]:
    archive = request.archive
    if archive is None:
        raise ImageBootstrapError(f"{request.label} archive is required")
    with tempfile.TemporaryDirectory(prefix="codeprobe-bootstrap-") as raw_directory:
        directory = Path(raw_directory)
        snapshot = directory / f"{request.label}.oci.tar"
        digest_path = directory / f"{request.label}.digest"
        _copy_regular_file(archive, snapshot, request.label)
        yield snapshot, digest_path


def _copy_regular_file(source: Path, destination: Path, label: str) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(source, flags)
    except OSError as exc:
        raise ImageBootstrapError(f"{label} archive must be a regular non-symlink file") from exc
    destination_fd = -1
    try:
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise ImageBootstrapError(f"{label} archive must be a regular non-symlink file")
        destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(source_fd, "rb") as source_stream:
            source_fd = -1
            with os.fdopen(destination_fd, "wb") as destination_stream:
                destination_fd = -1
                shutil.copyfileobj(source_stream, destination_stream)
                destination_stream.flush()
                os.fsync(destination_stream.fileno())
    except OSError as exc:
        raise ImageBootstrapError(f"{label} archive could not be copied into private storage") from exc
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if destination_fd >= 0:
            os.close(destination_fd)


def _read_copied_digest(label: str, path: Path) -> str:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 128:
            raise ImageBootstrapError(f"{label} copied image digest is invalid")
        path.chmod(0o600)
        digest = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ImageBootstrapError(f"{label} copied image digest is unavailable") from exc
    except UnicodeError as exc:
        raise ImageBootstrapError(f"{label} copied image digest is invalid") from exc
    if DIGEST_PATTERN.fullmatch(digest) is None:
        raise ImageBootstrapError(f"{label} copied image digest is invalid")
    return digest


def _verified_config_digest(label: str, raw_manifest: str, copied_digest: str) -> str:
    observed = "sha256:" + hashlib.sha256(raw_manifest.encode("utf-8")).hexdigest()
    if observed != copied_digest:
        raise ImageBootstrapError(f"{label} copied image digest mismatch")
    try:
        manifest = json.loads(raw_manifest)
    except json.JSONDecodeError as exc:
        raise ImageBootstrapError(f"{label} copied image manifest is invalid") from exc
    if not isinstance(manifest, dict):
        raise ImageBootstrapError(f"{label} copied image manifest is invalid")
    config = manifest.get("config")
    config_digest = config.get("digest") if isinstance(config, dict) else None
    if not isinstance(config_digest, str) or LOCAL_IMAGE_ID_PATTERN.fullmatch(config_digest) is None:
        raise ImageBootstrapError(f"{label} copied image manifest omitted a valid config digest")
    return config_digest


def _inspect_offline_image(
    request: _ImageRequest,
    engine_path: str,
    destination: str,
    copied_digest: str,
    config_digest: str,
    runner: CommandRunner,
) -> PreparedImage:
    output = runner([engine_path, "image", "inspect", destination], INSPECT_TIMEOUT_SECONDS)
    record = _single_inspect_record(request.label, output)
    local_id = _local_image_id(request.label, record)
    if local_id != config_digest:
        raise ImageBootstrapError(f"{request.label} image ID does not match its copied manifest")
    observed_digests = _observed_digests(record)
    if observed_digests and copied_digest not in observed_digests and request.digest not in observed_digests:
        raise ImageBootstrapError(f"{request.label} image digest mismatch")
    return PreparedImage(
        source_reference=request.source_reference,
        verified_reference=request.verified_reference,
        digest=request.digest,
        local_id=local_id,
    )


def _destination_reference(request: _ImageRequest) -> str:
    without_digest = request.source_reference.split("@", 1)[0]
    last_slash = without_digest.rfind("/")
    last_colon = without_digest.rfind(":")
    repository = without_digest[:last_colon] if last_colon > last_slash else without_digest
    destination = f"{repository}:codeprobe-{request.digest.removeprefix('sha256:')}"
    try:
        return validate_image_reference(f"{request.label} offline destination", destination)
    except ValueError as exc:
        raise ImageBootstrapError(str(exc)) from exc


def _inspect_prepared_image(
    request: _ImageRequest,
    engine_path: str,
    reference: str,
    runner: CommandRunner,
) -> PreparedImage:
    output = runner([engine_path, "image", "inspect", reference], INSPECT_TIMEOUT_SECONDS)
    record = _single_inspect_record(request.label, output)
    observed_digests = _observed_digests(record)
    if request.digest not in observed_digests:
        raise ImageBootstrapError(f"{request.label} image digest mismatch")
    local_id = _local_image_id(request.label, record)
    return PreparedImage(
        source_reference=request.source_reference,
        verified_reference=request.verified_reference,
        digest=request.digest,
        local_id=local_id,
    )


def _single_inspect_record(label: str, output: str) -> dict[str, object]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ImageBootstrapError(f"{label} image inspect returned invalid JSON") from exc
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise ImageBootstrapError(f"{label} image inspect returned an invalid record")
    return payload[0]


def _observed_digests(record: dict[str, object]) -> frozenset[str]:
    observed: set[str] = set()
    digest = record.get("Digest")
    if isinstance(digest, str):
        observed.add(digest)
    repo_digests = record.get("RepoDigests")
    if isinstance(repo_digests, list):
        observed.update(item.rsplit("@", 1)[1] for item in repo_digests if isinstance(item, str) and "@" in item)
    return frozenset(observed)


def _local_image_id(label: str, record: dict[str, object]) -> str:
    raw = record.get("Id", record.get("ID"))
    if not isinstance(raw, str):
        raise ImageBootstrapError(f"{label} image inspect omitted the local image ID")
    normalized = raw if raw.startswith("sha256:") else f"sha256:{raw}"
    if LOCAL_IMAGE_ID_PATTERN.fullmatch(normalized) is None:
        raise ImageBootstrapError(f"{label} image inspect returned an invalid local ID")
    return normalized


def _validate_archive_pair(agent_archive: Path | None, scoring_archive: Path | None) -> None:
    if (agent_archive is None) != (scoring_archive is None):
        raise ImageBootstrapError("Supply both agent and scoring archives for offline preparation")


def _run_text_command(command: Sequence[str], timeout: float) -> str:
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            completed = subprocess.run(
                list(command),
                check=False,
                stdout=stdout,
                stderr=stderr,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ImageBootstrapError(f"{command[0]} timed out after {timeout:g}s") from exc
        except OSError as exc:
            raise ImageBootstrapError(f"{command[0]} failed to launch") from exc
        if completed.returncode != 0:
            raise ImageBootstrapError(f"{command[0]} failed with exit {completed.returncode}")
        stdout.seek(0)
        output = stdout.read(_MAX_COMMAND_OUTPUT_BYTES + 1)
    if len(output) > _MAX_COMMAND_OUTPUT_BYTES:
        raise ImageBootstrapError(f"{command[0]} returned oversized output")
    try:
        return output.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ImageBootstrapError(f"{command[0]} returned non-UTF-8 output") from exc
