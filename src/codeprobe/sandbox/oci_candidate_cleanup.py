"""Failure-only cleanup for run-unique OCI candidate tags."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from codeprobe.sandbox.oci_references import validate_image_reference

COMMAND_TIMEOUT_SECONDS: Final[float] = 120.0
EXPECTED_IMAGES: Final[tuple[str, ...]] = ("codeprobe-agent", "codeprobe-scoring")
_DIGEST_RE: Final[re.Pattern[str]] = re.compile(r"sha256:[a-f0-9]{64}\Z")
_SHA_RE: Final[re.Pattern[str]] = re.compile(r"[a-f0-9]{40}\Z")
_ABSENT_RE: Final[re.Pattern[str]] = re.compile(
    r"(manifest unknown|name unknown|MANIFEST_UNKNOWN|NAME_UNKNOWN)", re.I
)


class CandidateCleanupError(RuntimeError):
    """Raised when a candidate tag cannot be safely cleaned up."""


class CandidateCommandError(CandidateCleanupError):
    """Raised for bounded external command failures."""

    def __init__(self, label: str, returncode: int, stderr: str) -> None:
        super().__init__(f"{label} failed with exit {returncode}")
        self.stderr = stderr


@dataclass(frozen=True)
class CleanupMetadata:
    candidate_ref: str
    image: str
    version: str
    source_sha: str
    build_digest: str
    digest_ref: str
    repository: str
    ref: str
    run_id: str
    run_attempt: str


CommandRunner = Callable[[Sequence[str], float], str]


def cleanup_candidate(
    metadata: CleanupMetadata,
    output_dir: Path,
    runner: CommandRunner | None = None,
) -> bool:
    """Delete a failed run-unique candidate tag or write quarantine evidence."""

    runner = runner or _run_text_command
    _validate_metadata(metadata)
    return _cleanup_observed_candidate(metadata, output_dir, runner)


def _cleanup_observed_candidate(
    metadata: CleanupMetadata, output_dir: Path, runner: CommandRunner
) -> bool:
    observed = _safe_resolve(metadata, output_dir, runner)
    if observed is None:
        print(f"candidate cleanup: candidate already absent: {metadata.candidate_ref}")
        return False
    if observed == "":
        return True
    if metadata.build_digest and observed != metadata.build_digest:
        _write_quarantine(output_dir, "candidate-digest-mismatch", metadata, observed)
        return True
    if _shared_digest_quarantined(metadata, output_dir, observed, runner):
        return True
    if not _safe_delete(metadata, output_dir, observed, runner):
        return True
    after_delete = _safe_resolve(metadata, output_dir, runner)
    if after_delete is None:
        print(f"candidate cleanup: deleted failed candidate {metadata.candidate_ref}")
        return False
    if after_delete == "":
        return True
    _write_quarantine(output_dir, "candidate-delete-not-proven", metadata, after_delete)
    return True


def _safe_resolve(
    metadata: CleanupMetadata, output_dir: Path, runner: CommandRunner
) -> str | None:
    try:
        return _resolve_optional(metadata.candidate_ref, runner)
    except CandidateCleanupError:
        _write_quarantine(output_dir, "candidate-resolve-failed", metadata, "")
        return ""


def _safe_delete(
    metadata: CleanupMetadata, output_dir: Path, observed: str, runner: CommandRunner
) -> bool:
    try:
        _delete_candidate(metadata.candidate_ref, runner)
    except CandidateCleanupError:
        _write_quarantine(output_dir, "candidate-delete-failed", metadata, observed)
        return False
    return True


def _shared_digest_quarantined(
    metadata: CleanupMetadata,
    output_dir: Path,
    observed_digest: str,
    runner: CommandRunner,
) -> bool:
    try:
        shared_ref = _shared_digest_ref(metadata.candidate_ref, observed_digest, runner)
    except CandidateCleanupError:
        _write_quarantine(output_dir, "candidate-tag-scan-failed", metadata, observed_digest)
        return True
    if shared_ref is None:
        return False
    _write_quarantine(
        output_dir,
        "candidate-digest-shared",
        metadata,
        observed_digest,
        {"shared_ref": shared_ref},
    )
    return True


def _shared_digest_ref(
    candidate_ref: str, observed_digest: str, runner: CommandRunner
) -> str | None:
    repository, candidate_tag = _repository_and_tag(candidate_ref)
    for tag in _list_repository_tags(repository, runner):
        if tag == candidate_tag:
            continue
        ref_name = f"{repository}:{tag}"
        if _resolve_required(ref_name, runner) == observed_digest:
            return ref_name
    return None


def _repository_and_tag(ref_name: str) -> tuple[str, str]:
    if ":" not in ref_name:
        raise CandidateCleanupError("candidate ref must include a tag")
    repository, tag = ref_name.rsplit(":", 1)
    if not repository or not tag or "/" not in repository:
        raise CandidateCleanupError("candidate ref must include a repository and tag")
    return repository, tag


def _validate_metadata(metadata: CleanupMetadata) -> None:
    repository, tag = _repository_and_tag(metadata.candidate_ref)
    expected_tag = (
        f"{metadata.version}-{metadata.run_id}-"
        f"{metadata.run_attempt}-{metadata.source_sha[:12]}"
    )
    if metadata.image not in EXPECTED_IMAGES:
        raise CandidateCleanupError("cleanup metadata image is unsupported")
    if not _SHA_RE.fullmatch(metadata.source_sha):
        raise CandidateCleanupError("cleanup metadata source sha is invalid")
    if not metadata.run_id.isdigit() or not metadata.run_attempt.isdigit():
        raise CandidateCleanupError("cleanup metadata run identity is invalid")
    if tag != expected_tag:
        raise CandidateCleanupError("cleanup candidate tag does not match metadata")
    if repository.rsplit("/", 1)[-1] != metadata.image:
        raise CandidateCleanupError("cleanup candidate repository does not match image")
    _validate_cleanup_ref("cleanup candidate ref", metadata.candidate_ref)
    _validate_digest_contract(metadata, repository)


def _validate_digest_contract(metadata: CleanupMetadata, repository: str) -> None:
    if metadata.build_digest and not _DIGEST_RE.fullmatch(metadata.build_digest):
        raise CandidateCleanupError("cleanup metadata build digest is invalid")
    expected_digest_ref = (
        f"{repository}@{metadata.build_digest}" if metadata.build_digest else ""
    )
    if metadata.digest_ref != expected_digest_ref:
        raise CandidateCleanupError("cleanup metadata digest ref is invalid")
    if metadata.digest_ref:
        _validate_cleanup_ref("cleanup digest ref", metadata.digest_ref)


def _validate_cleanup_ref(name: str, ref_name: str) -> None:
    try:
        validate_image_reference(name, ref_name)
    except ValueError as exc:
        raise CandidateCleanupError(f"{name} is invalid") from exc


def _list_repository_tags(repository: str, runner: CommandRunner) -> list[str]:
    output = runner(["oras", "repo", "tags", repository], COMMAND_TIMEOUT_SECONDS)
    tags = [line.strip() for line in output.splitlines() if line.strip()]
    if not tags:
        raise CandidateCleanupError("repository tag list was empty")
    return tags


def _resolve_required(ref_name: str, runner: CommandRunner) -> str:
    digest = runner(["oras", "resolve", ref_name], COMMAND_TIMEOUT_SECONDS).strip()
    if not _DIGEST_RE.fullmatch(digest):
        raise CandidateCleanupError("tag resolve returned invalid digest")
    return digest


def _delete_candidate(candidate_ref: str, runner: CommandRunner) -> None:
    try:
        runner(
            ["oras", "manifest", "delete", "--force", candidate_ref],
            COMMAND_TIMEOUT_SECONDS,
        )
    except CandidateCommandError as exc:
        if _ABSENT_RE.search(exc.stderr):
            return
        raise


def _resolve_optional(candidate_ref: str, runner: CommandRunner) -> str | None:
    try:
        digest = runner(["oras", "resolve", candidate_ref], COMMAND_TIMEOUT_SECONDS).strip()
    except CandidateCommandError as exc:
        if _ABSENT_RE.search(exc.stderr):
            return None
        raise
    if not _DIGEST_RE.fullmatch(digest):
        raise CandidateCleanupError("candidate resolve returned invalid digest")
    return digest


def _write_quarantine(
    output_dir: Path,
    reason: str,
    metadata: CleanupMetadata,
    observed_digest: str,
    extra: dict[str, object] | None = None,
) -> None:
    payload = _quarantine_payload(reason, metadata, observed_digest, extra)
    temporary_path: Path | None = None
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / "candidate-quarantine.json"
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_dir,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            output.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, output_path)
    except OSError as exc:
        raise CandidateCleanupError(
            "could not persist candidate quarantine evidence"
        ) from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError as exc:
                raise CandidateCleanupError(
                    "could not persist candidate quarantine evidence"
                ) from exc


def _quarantine_payload(
    reason: str,
    metadata: CleanupMetadata,
    observed_digest: str,
    extra: dict[str, object] | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "candidate_quarantine_schema": 1,
        "reason": reason,
        "deletion_proven": False,
        "candidate_ref": metadata.candidate_ref,
        "observed_digest": observed_digest,
        "build_digest": metadata.build_digest,
        "digest_ref": metadata.digest_ref,
        "image": metadata.image,
        "version": metadata.version,
        "source_sha": metadata.source_sha,
        "run": {
            "repository": metadata.repository,
            "ref": metadata.ref,
            "run_id": metadata.run_id,
            "run_attempt": metadata.run_attempt,
        },
    }
    if extra:
        payload.update(extra)
    return payload


def _run_text_command(command: Sequence[str], timeout: float) -> str:
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CandidateCleanupError(
            f"{command[0]} timed out after {timeout:g}s"
        ) from exc
    except OSError as exc:
        raise CandidateCleanupError(f"{command[0]} failed to launch") from exc
    if completed.returncode != 0:
        raise CandidateCommandError(command[0], completed.returncode, completed.stderr)
    return completed.stdout


def _metadata_from_args(args: argparse.Namespace) -> CleanupMetadata:
    return CleanupMetadata(
        candidate_ref=args.candidate_ref,
        image=args.image,
        version=args.version,
        source_sha=args.source_sha,
        build_digest=args.build_digest,
        digest_ref=args.digest_ref,
        repository=os.environ.get("GITHUB_REPOSITORY", ""),
        ref=os.environ.get("GITHUB_REF", ""),
        run_id=os.environ.get("GITHUB_RUN_ID", ""),
        run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", ""),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Delete failed OCI candidate tags.")
    parser.add_argument("--candidate-ref", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--build-digest", default="")
    parser.add_argument("--digest-ref", default="")
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        quarantined = cleanup_candidate(_metadata_from_args(args), args.output_dir)
    except CandidateCleanupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if quarantined:
        print("FATAL: candidate cleanup could not prove safe deletion", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
