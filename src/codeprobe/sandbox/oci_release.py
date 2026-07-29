"""Release-pair authority helpers for OCI execution images."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Final

from codeprobe.sandbox.oci_attestations import (
    AttestationVerificationError,
    _docker_raw_manifest,
    _oras_blob_fetch,
    verify_buildkit_attestations,
)
from codeprobe.sandbox.oci_release_contract import (
    EXPECTED_IMAGES,
    REQUIRED_PLATFORMS,
    ImageIdentity,
    OciReleaseError,
    build_pair,
    image_repo,
    load_identities,
    load_pair_identities,
    single_version,
    validate_identity_contracts,
    validate_promotion_state,
)

OIDC_ISSUER: Final[str] = "https://token.actions.githubusercontent.com"
RELEASE_PAIR_ARTIFACT_TYPE: Final[str] = "application/vnd.codeprobe.release-pair.v1+json"
PROMOTION_STATE_ARTIFACT_TYPE: Final[str] = (
    "application/vnd.codeprobe.promotion-state.v1+json"
)
SIGSTORE_BUNDLE_TYPE: Final[str] = "application/vnd.dev.sigstore.bundle.v0.3+json"
COMMAND_TIMEOUT_SECONDS: Final[float] = 120.0
TRIVY_TIMEOUT_SECONDS: Final[float] = 600.0
_DIGEST_RE: Final[re.Pattern[str]] = re.compile(r"sha256:[a-f0-9]{64}\Z")
_ABSENT_RE: Final[re.Pattern[str]] = re.compile(
    r"(manifest unknown|name unknown|MANIFEST_UNKNOWN|NAME_UNKNOWN)", re.I
)


class OciCommandError(OciReleaseError):
    """Raised for bounded external command failures."""

    def __init__(self, label: str, returncode: int, stderr: str) -> None:
        super().__init__(f"{label} failed with exit {returncode}")
        self.stderr = stderr


CommandRunner = Callable[[Sequence[str], float], str]


def pair_ref(registry: str, namespace: str, version: str) -> str:
    raw = f"{registry}/{namespace}/codeprobe-release-pair:{version}"
    return raw.lower()


def check_reuse(
    *,
    registry: str,
    namespace: str,
    version: str,
    repository: str,
    ref: str,
    source_sha: str,
    cert_identity: str,
    output_dir: Path,
    trivy_image: str,
    trivy_severity: str,
    runner: CommandRunner | None = None,
) -> bool:
    runner = runner or _run_text_command
    ref_name = pair_ref(registry, namespace, version)
    pair_digest = _resolve_optional(ref_name, runner)
    if pair_digest is None:
        _require_version_tags_absent(registry, namespace, version, runner)
        return False
    pair_dir = output_dir / "release-pair"
    _pull_pair(_tag_ref_to_digest_ref(ref_name, pair_digest), pair_dir, runner)
    _verify_pair_bundle(pair_dir, cert_identity, runner)
    identities = load_pair_identities(
        pair_dir / "release-pair.json", repository, ref, source_sha, version
    )
    validate_identity_contracts(
        identities, registry=registry, namespace=namespace, version=version,
        source_sha=source_sha
    )
    for identity in identities:
        _verify_image_identity(identity, source_sha, runner)
        _verify_image_signature(identity, repository, ref, source_sha, cert_identity, runner)
        _scan_image_platforms(identity, trivy_image, trivy_severity, runner)
    _require_tag_still_at_digest(ref_name, pair_digest, runner)
    _write_reuse_evidence(output_dir, ref_name, pair_digest, identities)
    return True


def promote_tags(
    *,
    identity_dir: Path,
    state_path: Path,
    registry: str,
    namespace: str,
    version: str,
    source_sha: str,
    runner: CommandRunner | None = None,
) -> str:
    runner = runner or _run_text_command
    identities = load_identities(identity_dir)
    validate_identity_contracts(
        identities,
        registry=registry,
        namespace=namespace,
        version=version,
        source_sha=source_sha,
    )
    _verify_candidate_digests(identities, runner)
    existing = {
        item.tag_ref: _inspect_digest_optional(item.tag_ref, runner)
        for item in identities
    }
    if any(digest is not None for digest in existing.values()):
        raise OciReleaseError("version tags exist without signed release-pair authority")
    promoted: list[dict[str, str]] = []
    state: dict[str, object] = {
        "promotion_state_schema": 1,
        "version_tag_state": "new",
        "promoted": promoted,
    }
    _write_json(state_path, state)
    for identity in identities:
        _promote_identity(identity, runner)
        promoted.append(
            {"tag_ref": identity.tag_ref, "digest_ref": identity.digest_ref}
        )
        _write_json(state_path, state)
    _verify_tag_digests(identities, runner)
    return "new"


def _promote_identity(identity: ImageIdentity, runner: CommandRunner) -> None:
    runner(
        [
            "docker",
            "buildx",
            "imagetools",
            "create",
            "--tag",
            identity.tag_ref,
            identity.digest_ref,
        ],
        COMMAND_TIMEOUT_SECONDS,
    )


def publish_pair(
    *,
    identity_dir: Path,
    promotion_state_path: Path,
    registry: str,
    namespace: str,
    repository: str,
    ref: str,
    source_sha: str,
    cert_identity: str,
    output_dir: Path,
    runner: CommandRunner | None = None,
) -> str:
    runner = runner or _run_text_command
    identities = load_identities(identity_dir)
    version = single_version(identities)
    validate_identity_contracts(
        identities, registry=registry, namespace=namespace, version=version,
        source_sha=source_sha
    )
    validate_promotion_state(promotion_state_path, identities)
    _verify_tag_digests(identities, runner)
    pair_path = output_dir / "release-pair.json"
    bundle_path = output_dir / "release-pair.bundle"
    _write_json(pair_path, build_pair(repository, ref, source_sha, identities))
    ref_name = pair_ref(registry, namespace, version)
    if _pair_exists(ref_name, runner):
        _verify_existing_pair(ref_name, output_dir, pair_path, cert_identity, runner)
    else:
        _sign_and_push_pair(
            ref_name, pair_path, bundle_path, promotion_state_path, cert_identity, runner
        )
    digest = _resolve_ref(ref_name, runner)
    _verify_pushed_pair_by_digest(
        ref_name, digest, output_dir, pair_path, cert_identity, runner
    )
    _write_json(
        output_dir / "release-pair-ref.json",
        {
            "release_pair_ref_schema": 1,
            "ref": ref_name,
            "digest": digest,
            "digest_ref": _tag_ref_to_digest_ref(ref_name, digest),
        },
    )
    return ref_name


def write_promotion_quarantine(
    *,
    identity_dir: Path,
    output_path: Path,
    source_sha: str,
    runner: CommandRunner | None = None,
) -> None:
    runner = runner or _run_text_command
    identities = load_identities(identity_dir, strict=False)
    observed = _promotion_observed_state(identities, runner)
    _write_json(
        output_path,
        {
            "quarantine_schema": 1,
            "reason": "partial-promotion-or-verification-failure",
            "source_sha": source_sha,
            "identities": [item.as_json() for item in identities],
            "observed_version_tags": observed,
        },
    )


def _verify_image_identity(
    identity: ImageIdentity, source_sha: str, runner: CommandRunner
) -> None:
    if identity.source_sha != source_sha:
        raise OciReleaseError(f"{identity.image} identity does not match release source")
    actual = _inspect_digest(identity.tag_ref, runner)
    if actual != identity.digest:
        raise OciReleaseError(f"{identity.image} immutable tag digest mismatch")
    verify_buildkit_attestations(
        image_ref=identity.image_ref,
        candidate_ref=identity.candidate_ref,
        digest_ref=identity.digest_ref,
        raw_manifest=_docker_raw_manifest,
        blob_fetch=_oras_blob_fetch,
    )


def _verify_image_signature(
    identity: ImageIdentity,
    repository: str,
    ref: str,
    source_sha: str,
    cert_identity: str,
    runner: CommandRunner,
) -> None:
    runner(
        [
            "cosign",
            "verify",
            "--certificate-identity",
            cert_identity,
            "--certificate-oidc-issuer",
            OIDC_ISSUER,
            identity.digest_ref,
        ],
        COMMAND_TIMEOUT_SECONDS,
    )
    runner(
        [
            "gh",
            "attestation",
            "verify",
            f"oci://{identity.digest_ref}",
            "--repo",
            repository,
            "--cert-identity",
            cert_identity,
            "--cert-oidc-issuer",
            OIDC_ISSUER,
            "--source-ref",
            ref,
            "--source-digest",
            source_sha,
            "--bundle-from-oci",
        ],
        COMMAND_TIMEOUT_SECONDS,
    )


def _scan_image_platforms(
    identity: ImageIdentity, trivy_image: str, severity: str, runner: CommandRunner
) -> None:
    for platform in REQUIRED_PLATFORMS:
        runner(
            [
                "docker",
                "run",
                "--rm",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--pids-limit=256",
                "--memory=4g",
                "--memory-swap=4g",
                "--cpus=2",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=256m",
                "--tmpfs",
                "/root/.cache:rw,nosuid,nodev,size=2g",
                "-e",
                "TRIVY_USERNAME",
                "-e",
                "TRIVY_PASSWORD",
                trivy_image,
                "image",
                "--ignore-unfixed",
                "--exit-code",
                "1",
                "--severity",
                severity,
                "--platform",
                platform,
                identity.digest_ref,
            ],
            TRIVY_TIMEOUT_SECONDS,
        )


def _pair_exists(ref_name: str, runner: CommandRunner) -> bool:
    try:
        runner(["oras", "manifest", "fetch", ref_name], COMMAND_TIMEOUT_SECONDS)
    except OciCommandError as exc:
        if _ABSENT_RE.search(exc.stderr):
            return False
        raise
    return True


def _require_version_tags_absent(
    registry: str, namespace: str, version: str, runner: CommandRunner
) -> None:
    existing = [
        ref_name
        for ref_name in (
            f"{image_repo(registry, namespace, image)}:{version}"
            for image in EXPECTED_IMAGES
        )
        if _inspect_digest_optional(ref_name, runner) is not None
    ]
    if existing:
        raise OciReleaseError(
            "version tag exists without signed release-pair authority"
        )


def _pull_pair(ref_name: str, pair_dir: Path, runner: CommandRunner) -> None:
    pair_dir.mkdir(parents=True, exist_ok=True)
    runner(
        ["oras", "pull", ref_name, "--output", str(pair_dir)],
        COMMAND_TIMEOUT_SECONDS,
    )


def _verify_pair_bundle(pair_dir: Path, cert_identity: str, runner: CommandRunner) -> None:
    runner(
        [
            "cosign",
            "verify-blob",
            "--bundle",
            str(pair_dir / "release-pair.bundle"),
            "--certificate-identity",
            cert_identity,
            "--certificate-oidc-issuer",
            OIDC_ISSUER,
            str(pair_dir / "release-pair.json"),
        ],
        COMMAND_TIMEOUT_SECONDS,
    )


def _verify_existing_pair(
    ref_name: str,
    output_dir: Path,
    pair_path: Path,
    cert_identity: str,
    runner: CommandRunner,
) -> None:
    digest = _resolve_ref(ref_name, runner)
    existing_dir = output_dir / "release-pair-existing"
    _pull_pair(_tag_ref_to_digest_ref(ref_name, digest), existing_dir, runner)
    _compare_pair_file(existing_dir / "release-pair.json", pair_path)
    _verify_pair_bundle(existing_dir, cert_identity, runner)
    _require_tag_still_at_digest(ref_name, digest, runner)
    _copy_existing_bundle(existing_dir / "release-pair.bundle", output_dir)


def _compare_pair_file(existing_path: Path, expected_path: Path) -> None:
    if _read_json_file(existing_path, "existing release pair") != _read_json_file(
        expected_path, "rebuilt release pair"
    ):
        raise OciReleaseError("existing release pair does not match rebuilt identity")


def _read_json_file(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise OciReleaseError(f"could not read {label}") from exc
    except json.JSONDecodeError as exc:
        raise OciReleaseError(f"malformed {label} JSON") from exc


def _copy_existing_bundle(bundle_path: Path, output_dir: Path) -> None:
    try:
        bundle = bundle_path.read_bytes()
        (output_dir / "release-pair.bundle").write_bytes(bundle)
    except OSError as exc:
        raise OciReleaseError("could not read existing release pair bundle") from exc


def _sign_and_push_pair(
    ref_name: str,
    pair_path: Path,
    bundle_path: Path,
    promotion_state_path: Path,
    cert_identity: str,
    runner: CommandRunner,
) -> None:
    runner(
        ["cosign", "sign-blob", "--yes", "--bundle", str(bundle_path), str(pair_path)],
        COMMAND_TIMEOUT_SECONDS,
    )
    runner(
        [
            "cosign",
            "verify-blob",
            "--bundle",
            str(bundle_path),
            "--certificate-identity",
            cert_identity,
            "--certificate-oidc-issuer",
            OIDC_ISSUER,
            str(pair_path),
        ],
        COMMAND_TIMEOUT_SECONDS,
    )
    if _pair_exists(ref_name, runner):
        raise OciReleaseError("release pair ref appeared before push")
    runner(
        [
            "oras",
            "push",
            ref_name,
            "--artifact-type",
            RELEASE_PAIR_ARTIFACT_TYPE,
            f"{pair_path}:{RELEASE_PAIR_ARTIFACT_TYPE}",
            f"{bundle_path}:{SIGSTORE_BUNDLE_TYPE}",
            f"{promotion_state_path}:{PROMOTION_STATE_ARTIFACT_TYPE}",
        ],
        COMMAND_TIMEOUT_SECONDS,
    )


def _verify_pushed_pair_by_digest(
    ref_name: str,
    digest: str,
    output_dir: Path,
    pair_path: Path,
    cert_identity: str,
    runner: CommandRunner,
) -> None:
    digest_ref = _tag_ref_to_digest_ref(ref_name, digest)
    published_dir = output_dir / "release-pair-published"
    _pull_pair(digest_ref, published_dir, runner)
    _compare_pair_file(published_dir / "release-pair.json", pair_path)
    _verify_pair_bundle(published_dir, cert_identity, runner)
    _require_tag_still_at_digest(ref_name, digest, runner)


def _verify_tag_digests(identities: Iterable[ImageIdentity], runner: CommandRunner) -> None:
    for identity in identities:
        if _inspect_digest(identity.tag_ref, runner) != identity.digest:
            raise OciReleaseError(f"immutable tag drift for {identity.tag_ref}")


def _verify_candidate_digests(
    identities: Iterable[ImageIdentity], runner: CommandRunner
) -> None:
    for identity in identities:
        if _resolve_ref(identity.candidate_ref, runner) != identity.digest:
            raise OciReleaseError(f"candidate digest mismatch for {identity.image}")


def _inspect_digest(ref_name: str, runner: CommandRunner) -> str:
    output = runner(["docker", "buildx", "imagetools", "inspect", ref_name], COMMAND_TIMEOUT_SECONDS)
    match = re.search(r"^Digest:\s+(sha256:[a-f0-9]{64})\s*$", output, re.M)
    if match is None:
        raise OciReleaseError(f"could not read digest for {ref_name}")
    return match.group(1)


def _inspect_digest_optional(ref_name: str, runner: CommandRunner) -> str | None:
    try:
        return _inspect_digest(ref_name, runner)
    except OciCommandError as exc:
        if _ABSENT_RE.search(exc.stderr):
            return None
        raise


def _resolve_ref(ref_name: str, runner: CommandRunner) -> str:
    resolved = runner(["oras", "resolve", ref_name], COMMAND_TIMEOUT_SECONDS).strip()
    if not _DIGEST_RE.fullmatch(resolved):
        raise OciReleaseError(f"could not resolve digest for {ref_name}")
    return resolved


def _resolve_optional(ref_name: str, runner: CommandRunner) -> str | None:
    try:
        return _resolve_ref(ref_name, runner)
    except OciCommandError as exc:
        if _ABSENT_RE.search(exc.stderr):
            return None
        raise


def _require_tag_still_at_digest(
    ref_name: str, expected_digest: str, runner: CommandRunner
) -> None:
    if _resolve_ref(ref_name, runner) != expected_digest:
        raise OciReleaseError("release pair ref changed during verification")


def _write_reuse_evidence(
    output_dir: Path, ref_name: str, digest: str, identities: Iterable[ImageIdentity]
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        output_dir / "reuse-evidence.json",
        {
            "reuse_evidence_schema": 1,
            "reuse": True,
            "release_pair_ref": ref_name,
            "release_pair_digest": digest,
            "release_pair_digest_ref": _tag_ref_to_digest_ref(ref_name, digest),
            "images": [item.as_json() for item in identities],
        },
    )


def _write_json(path: Path, value: object) -> None:
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            output.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    except OSError as exc:
        raise OciReleaseError("could not write JSON output") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError as exc:
                raise OciReleaseError("could not write JSON output") from exc


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
        raise OciReleaseError(f"{command[0]} timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise OciReleaseError(f"{command[0]} failed to launch") from exc
    if completed.returncode != 0:
        raise OciCommandError(command[0], completed.returncode, completed.stderr)
    return completed.stdout


def _promotion_observed_state(
    identities: tuple[ImageIdentity, ...], runner: CommandRunner
) -> dict[str, object]:
    observed: dict[str, object] = {}
    for item in identities:
        try:
            observed[item.tag_ref] = _inspect_digest_optional(item.tag_ref, runner)
        except (OciReleaseError, AttestationVerificationError):
            observed[item.tag_ref] = {"state": "unknown", "error": "inspect-failed"}
    return observed


def _tag_ref_to_digest_ref(ref_name: str, digest: str) -> str:
    slash = ref_name.rfind("/")
    colon = ref_name.rfind(":")
    if colon <= slash:
        raise OciReleaseError("release pair ref must include a tag")
    return f"{ref_name[:colon]}@{digest}"


def _write_github_output(path: str | None, key: str, value: str) -> None:
    if path:
        try:
            with Path(path).open("a", encoding="utf-8") as output:
                print(f"{key}={value}", file=output)
        except OSError as exc:
            raise OciReleaseError("could not write GitHub output") from exc


def _add_common_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--registry", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--cert-identity", required=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage OCI release-pair authority.")
    subcommands = parser.add_subparsers(dest="command", required=True)
    reuse = subcommands.add_parser("check-reuse")
    _add_common_source_args(reuse)
    reuse.add_argument("--output-dir", type=Path, required=True)
    reuse.add_argument("--trivy-image", required=True)
    reuse.add_argument("--trivy-severity", required=True)
    reuse.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    promote = subcommands.add_parser("promote-tags")
    promote.add_argument("--identity-dir", type=Path, required=True)
    promote.add_argument("--state-path", type=Path, required=True)
    promote.add_argument("--registry", required=True)
    promote.add_argument("--namespace", required=True)
    promote.add_argument("--version", required=True)
    promote.add_argument("--source-sha", required=True)
    promote.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    publish = subcommands.add_parser("publish-pair")
    _add_common_source_args(publish)
    publish.add_argument("--identity-dir", type=Path, required=True)
    publish.add_argument("--promotion-state-path", type=Path, required=True)
    publish.add_argument("--output-dir", type=Path, required=True)
    quarantine = subcommands.add_parser("quarantine-promotion")
    quarantine.add_argument("--identity-dir", type=Path, required=True)
    quarantine.add_argument("--output-path", type=Path, required=True)
    quarantine.add_argument("--source-sha", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "check-reuse":
            reused = _run_check_reuse(args)
            _write_github_output(args.github_output, "reuse", str(reused).lower())
        elif args.command == "promote-tags":
            state = _run_promote_tags(args)
            _write_github_output(args.github_output, "version_tag_state", state)
        elif args.command == "publish-pair":
            _run_publish_pair(args)
        else:
            write_promotion_quarantine(
                identity_dir=args.identity_dir,
                output_path=args.output_path,
                source_sha=args.source_sha,
            )
    except (OciReleaseError, AttestationVerificationError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def _run_check_reuse(args: argparse.Namespace) -> bool:
    return check_reuse(
        registry=args.registry,
        namespace=args.namespace,
        version=args.version,
        repository=args.repository,
        ref=args.ref,
        source_sha=args.source_sha,
        cert_identity=args.cert_identity,
        output_dir=args.output_dir,
        trivy_image=args.trivy_image,
        trivy_severity=args.trivy_severity,
    )


def _run_promote_tags(args: argparse.Namespace) -> str:
    return promote_tags(
        identity_dir=args.identity_dir,
        state_path=args.state_path,
        registry=args.registry,
        namespace=args.namespace,
        version=args.version,
        source_sha=args.source_sha,
    )


def _run_publish_pair(args: argparse.Namespace) -> None:
    publish_pair(
        identity_dir=args.identity_dir,
        promotion_state_path=args.promotion_state_path,
        registry=args.registry,
        namespace=args.namespace,
        repository=args.repository,
        ref=args.ref,
        source_sha=args.source_sha,
        cert_identity=args.cert_identity,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main())
