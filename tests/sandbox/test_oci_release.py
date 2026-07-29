"""Tests for OCI release-pair authority helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest

from codeprobe.sandbox import oci_release
from codeprobe.sandbox.oci_release import (
    COMMAND_TIMEOUT_SECONDS,
    ImageIdentity,
    OciCommandError,
    OciReleaseError,
    _run_text_command,
    check_reuse,
    promote_tags,
    publish_pair,
)

SHA: Final[str] = "a" * 40
PAIR_DIGEST: Final[str] = "sha256:" + "9" * 64
AGENT_DIGEST: Final[str] = "sha256:" + "1" * 64
SCORING_DIGEST: Final[str] = "sha256:" + "2" * 64
CERT_IDENTITY: Final[str] = (
    "https://github.com/sjarmak/codeprobe/.github/workflows/"
    "publish-images.yml@refs/tags/v1.2.3"
)


class RecordingRunner:
    def __init__(
        self,
        pair: dict[str, object] | None = None,
        *,
        version_tags_exist: bool = False,
        moved_pair_tag: bool = False,
        moved_version_tags: bool = False,
    ) -> None:
        self.calls: list[tuple[list[str], float]] = []
        self.pair = pair
        self.version_tags_exist = version_tags_exist
        self.moved_pair_tag = moved_pair_tag
        self.moved_version_tags = moved_version_tags
        self.pair_resolves = 0
        self.version_tag_inspects = 0
        self.created_version_tags: set[str] = set()

    def __call__(self, command: list[str], timeout: float) -> str:
        self.calls.append((command, timeout))
        if command[:3] == ["oras", "manifest", "fetch"]:
            if self.pair is None:
                raise OciCommandError("oras", 1, "manifest unknown")
            return "{}"
        if command[:2] == ["oras", "pull"]:
            output_dir = Path(command[command.index("--output") + 1])
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "release-pair.json").write_text(
                json.dumps(self.pair, sort_keys=True), encoding="utf-8"
            )
            (output_dir / "release-pair.bundle").write_text("bundle", encoding="utf-8")
            return ""
        if command[:2] == ["oras", "resolve"]:
            return self._resolve(command[-1]) + "\n"
        if command[:4] == ["docker", "buildx", "imagetools", "inspect"]:
            if not self.version_tags_exist and command[-1] not in self.created_version_tags:
                raise OciCommandError("docker", 1, "manifest unknown")
            self.version_tag_inspects += 1
            if self.moved_version_tags and self.version_tag_inspects > 2:
                return "Digest: sha256:" + "7" * 64 + "\n"
            return _inspect_output(command[-1])
        if command[:4] == ["docker", "buildx", "imagetools", "create"]:
            self.created_version_tags.add(command[command.index("--tag") + 1])
            return ""
        if command[:2] == ["oras", "push"]:
            self.pair = _pair()
            return ""
        return ""

    def _resolve(self, ref: str) -> str:
        if "codeprobe-release-pair" in ref:
            if self.pair is None:
                raise OciCommandError("oras", 1, "manifest unknown")
            self.pair_resolves += 1
            if self.moved_pair_tag and self.pair_resolves > 1 and "@" not in ref:
                return "sha256:" + "8" * 64
            return PAIR_DIGEST
        if "codeprobe-agent" in ref:
            self._require_version_tag_available(ref)
            return AGENT_DIGEST
        if "codeprobe-scoring" in ref:
            self._require_version_tag_available(ref)
            return SCORING_DIGEST
        return PAIR_DIGEST

    def _require_version_tag_available(self, ref: str) -> None:
        if not ref.endswith(":1.2.3"):
            return
        if not self.version_tags_exist and ref not in self.created_version_tags:
            raise OciCommandError("oras", 1, "manifest unknown")


def _identity(image: str, digest: str) -> ImageIdentity:
    return ImageIdentity(
        image=image,
        version="1.2.3",
        tag_ref=f"ghcr.io/sjarmak/codeprobe/{image}:1.2.3",
        candidate_ref=f"ghcr.io/sjarmak/codeprobe/{image}:1.2.3-1-1-{SHA[:12]}",
        digest=digest,
        digest_ref=f"ghcr.io/sjarmak/codeprobe/{image}@{digest}",
        platforms=("linux/amd64", "linux/arm64"),
        runtime_override_env="CODEPROBE_AGENT_IMAGE"
        if image == "codeprobe-agent"
        else "CODEPROBE_SCORING_IMAGE",
        source_sha=SHA,
    )


def _pair() -> dict[str, object]:
    return oci_release.build_pair(
        "sjarmak/codeprobe",
        "refs/tags/v1.2.3",
        SHA,
        [_identity("codeprobe-agent", AGENT_DIGEST), _identity("codeprobe-scoring", SCORING_DIGEST)],
    )


def _write_identities_and_state(tmp_path: Path) -> tuple[Path, Path]:
    identities = (
        _identity("codeprobe-agent", AGENT_DIGEST),
        _identity("codeprobe-scoring", SCORING_DIGEST),
    )
    identity_dir = _write_identity_files(tmp_path, identities)
    state_path = tmp_path / "promotion-state.json"
    state_path.write_text(
        json.dumps(
            {
                "promotion_state_schema": 1,
                "version_tag_state": "new",
                "promoted": [
                    {"tag_ref": identity.tag_ref, "digest_ref": identity.digest_ref}
                    for identity in identities
                ],
            }
        ),
        encoding="utf-8",
    )
    return identity_dir, state_path


def _write_identities(tmp_path: Path) -> Path:
    identities = (
        _identity("codeprobe-agent", AGENT_DIGEST),
        _identity("codeprobe-scoring", SCORING_DIGEST),
    )
    return _write_identity_files(tmp_path, identities)


def _write_identity_files(
    tmp_path: Path, identities: tuple[ImageIdentity, ...]
) -> Path:
    identity_dir = tmp_path / "identities"
    identity_dir.mkdir()
    for identity in identities:
        (identity_dir / f"{identity.image}.json").write_text(
            json.dumps(identity.as_json()), encoding="utf-8"
        )
    return identity_dir


def _inspect_output(ref: str) -> str:
    if "codeprobe-agent" in ref:
        return f"Name: {ref}\nDigest: {AGENT_DIGEST}\n"
    if "codeprobe-scoring" in ref:
        return f"Name: {ref}\nDigest: {SCORING_DIGEST}\n"
    return f"Digest: {PAIR_DIGEST}\n"


def test_check_reuse_outputs_false_when_pair_is_absent(tmp_path: Path) -> None:
    runner = RecordingRunner(pair=None)

    reused = check_reuse(
        registry="ghcr.io",
        namespace="sjarmak/codeprobe",
        version="1.2.3",
        repository="sjarmak/codeprobe",
        ref="refs/tags/v1.2.3",
        source_sha=SHA,
        cert_identity=CERT_IDENTITY,
        output_dir=tmp_path,
        trivy_image="trivy@sha256:abc",
        trivy_severity="CRITICAL,HIGH",
        runner=runner,
    )

    assert reused is False
    commands = [call[0] for call in runner.calls]
    assert commands[0][:2] == ["oras", "resolve"]
    version_resolves = [
        command
        for command in commands
        if command[:2] == ["oras", "resolve"]
        and command[-1].endswith(":1.2.3")
        and "release-pair" not in command[-1]
    ]
    assert len(version_resolves) == 2


def test_check_reuse_accepts_exact_oras_not_found_contract(tmp_path: Path) -> None:
    def runner(command: list[str], timeout: float) -> str:
        reference = command[-1]
        raise OciCommandError(
            "oras",
            1,
            (
                "Error response from registry: failed to resolve digest: "
                f"{reference}: not found"
            ),
        )

    assert (
        check_reuse(
            registry="ghcr.io",
            namespace="sjarmak/codeprobe",
            version="1.2.3",
            repository="sjarmak/codeprobe",
            ref="refs/tags/v1.2.3",
            source_sha=SHA,
            cert_identity=CERT_IDENTITY,
            output_dir=tmp_path,
            trivy_image="trivy@sha256:abc",
            trivy_severity="CRITICAL,HIGH",
            runner=runner,
        )
        is False
    )


def test_check_reuse_fails_closed_when_pair_absent_but_version_tag_exists(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner(pair=None, version_tags_exist=True)

    with pytest.raises(OciReleaseError, match="version tag exists"):
        check_reuse(
            registry="ghcr.io",
            namespace="sjarmak/codeprobe",
            version="1.2.3",
            repository="sjarmak/codeprobe",
            ref="refs/tags/v1.2.3",
            source_sha=SHA,
            cert_identity=CERT_IDENTITY,
            output_dir=tmp_path,
            trivy_image="trivy@sha256:abc",
            trivy_severity="CRITICAL,HIGH",
            runner=runner,
        )


def test_check_reuse_does_not_treat_auth_endpoint_404_as_absent(
    tmp_path: Path,
) -> None:
    def runner(command: list[str], timeout: float) -> str:
        raise OciCommandError("oras", 1, "authentication endpoint returned 404")

    with pytest.raises(OciCommandError):
        check_reuse(
            registry="ghcr.io",
            namespace="sjarmak/codeprobe",
            version="1.2.3",
            repository="sjarmak/codeprobe",
            ref="refs/tags/v1.2.3",
            source_sha=SHA,
            cert_identity=CERT_IDENTITY,
            output_dir=tmp_path,
            trivy_image="trivy@sha256:abc",
            trivy_severity="CRITICAL,HIGH",
            runner=runner,
        )


def test_check_reuse_does_not_treat_auth_error_with_absence_text_as_absent(
    tmp_path: Path,
) -> None:
    def runner(command: list[str], timeout: float) -> str:
        raise OciCommandError(
            "oras", 2, "UNAUTHORIZED: token rejected; manifest unknown is not proof"
        )

    with pytest.raises(OciCommandError):
        check_reuse(
            registry="ghcr.io",
            namespace="sjarmak/codeprobe",
            version="1.2.3",
            repository="sjarmak/codeprobe",
            ref="refs/tags/v1.2.3",
            source_sha=SHA,
            cert_identity=CERT_IDENTITY,
            output_dir=tmp_path,
            trivy_image="trivy@sha256:abc",
            trivy_severity="CRITICAL,HIGH",
            runner=runner,
        )

    assert not (tmp_path / "reuse-evidence.json").exists()


def test_check_reuse_verifies_existing_pair_and_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = RecordingRunner(pair=_pair(), version_tags_exist=True)
    attestation_calls: list[dict[str, str]] = []

    def verify_attestation(**kwargs: object) -> None:
        attestation_calls.append(
            {
                "candidate_ref": str(kwargs["candidate_ref"]),
                "digest_ref": str(kwargs["digest_ref"]),
            }
        )

    monkeypatch.setattr(oci_release, "verify_buildkit_attestations", verify_attestation)

    assert check_reuse(
        registry="ghcr.io",
        namespace="sjarmak/codeprobe",
        version="1.2.3",
        repository="sjarmak/codeprobe",
        ref="refs/tags/v1.2.3",
        source_sha=SHA,
        cert_identity=CERT_IDENTITY,
        output_dir=tmp_path,
        trivy_image="trivy@sha256:abc",
        trivy_severity="CRITICAL,HIGH",
        runner=runner,
    )

    commands = [call[0] for call in runner.calls]
    assert any(command[:2] == ["oras", "pull"] for command in commands)
    assert any(command[:2] == ["cosign", "verify-blob"] for command in commands)
    assert sum(command[:2] == ["cosign", "verify"] for command in commands) == 2
    assert sum(command[:3] == ["gh", "attestation", "verify"] for command in commands) == 2
    assert sum(command[:3] == ["docker", "run", "--rm"] for command in commands) == 4
    scan_commands = [
        command for command in commands if command[:3] == ["docker", "run", "--rm"]
    ]
    for command in scan_commands:
        _assert_hardened_trivy_command(command)
    assert {call["candidate_ref"] for call in attestation_calls} == {
        _identity("codeprobe-agent", AGENT_DIGEST).candidate_ref,
        _identity("codeprobe-scoring", SCORING_DIGEST).candidate_ref,
    }
    evidence = json.loads((tmp_path / "reuse-evidence.json").read_text())
    _assert_reuse_evidence(evidence)


def _assert_reuse_evidence(evidence: dict[str, object]) -> None:
    assert evidence["reuse"] is True
    assert evidence["release_pair_digest"] == PAIR_DIGEST
    assert evidence["release_pair_digest_ref"] == (
        "ghcr.io/sjarmak/codeprobe/codeprobe-release-pair@" + PAIR_DIGEST
    )


def _assert_hardened_trivy_command(command: list[str]) -> None:
    for option in (
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=256",
        "--memory=4g",
        "--memory-swap=4g",
        "--cpus=2",
        "--read-only",
        "/tmp:rw,noexec,nosuid,nodev,size=256m",
        "/root/.cache:rw,nosuid,nodev,size=2g",
    ):
        assert option in command


def test_check_reuse_fails_when_pair_tag_moves_during_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = RecordingRunner(
        pair=_pair(), version_tags_exist=True, moved_pair_tag=True
    )
    monkeypatch.setattr(oci_release, "verify_buildkit_attestations", lambda **_: None)

    with pytest.raises(OciReleaseError, match="changed during verification"):
        check_reuse(
            registry="ghcr.io",
            namespace="sjarmak/codeprobe",
            version="1.2.3",
            repository="sjarmak/codeprobe",
            ref="refs/tags/v1.2.3",
            source_sha=SHA,
            cert_identity=CERT_IDENTITY,
            output_dir=tmp_path,
            trivy_image="trivy@sha256:abc",
            trivy_severity="CRITICAL,HIGH",
            runner=runner,
        )


@pytest.mark.parametrize(
    "stderr",
    [
        "network unreachable: manifest unknown",
        "x509 certificate signed by unknown authority: manifest unknown",
        "HTTP 401: manifest unknown",
        "HTTP 403: manifest unknown",
        "HTTP 429: manifest unknown",
        "service unavailable: manifest unknown",
    ],
)
def test_check_reuse_rejects_prefixed_false_absence(
    tmp_path: Path, stderr: str
) -> None:
    def runner(command: list[str], timeout: float) -> str:
        raise OciCommandError("oras", 2, stderr)

    with pytest.raises(OciCommandError):
        check_reuse(
            registry="ghcr.io",
            namespace="sjarmak/codeprobe",
            version="1.2.3",
            repository="sjarmak/codeprobe",
            ref="refs/tags/v1.2.3",
            source_sha=SHA,
            cert_identity=CERT_IDENTITY,
            output_dir=tmp_path,
            trivy_image="trivy@sha256:abc",
            trivy_severity="CRITICAL,HIGH",
            runner=runner,
        )


def test_check_reuse_fails_when_version_tag_moves_during_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = RecordingRunner(
        pair=_pair(), version_tags_exist=True, moved_version_tags=True
    )
    monkeypatch.setattr(oci_release, "verify_buildkit_attestations", lambda **_: None)

    with pytest.raises(OciReleaseError, match="immutable tag drift"):
        check_reuse(
            registry="ghcr.io",
            namespace="sjarmak/codeprobe",
            version="1.2.3",
            repository="sjarmak/codeprobe",
            ref="refs/tags/v1.2.3",
            source_sha=SHA,
            cert_identity=CERT_IDENTITY,
            output_dir=tmp_path,
            trivy_image="trivy@sha256:abc",
            trivy_severity="CRITICAL,HIGH",
            runner=runner,
        )


def test_check_reuse_fails_closed_for_malformed_existing_pair(tmp_path: Path) -> None:
    runner = RecordingRunner(pair={"release_pair_schema": 1})

    with pytest.raises(OciReleaseError, match="schema"):
        check_reuse(
            registry="ghcr.io",
            namespace="sjarmak/codeprobe",
            version="1.2.3",
            repository="sjarmak/codeprobe",
            ref="refs/tags/v1.2.3",
            source_sha=SHA,
            cert_identity=CERT_IDENTITY,
            output_dir=tmp_path,
            trivy_image="trivy@sha256:abc",
            trivy_severity="CRITICAL,HIGH",
            runner=runner,
        )


def test_publish_pair_verifies_tags_before_signing(tmp_path: Path) -> None:
    identity_dir, state_path = _write_identities_and_state(tmp_path)
    runner = RecordingRunner(pair=None, version_tags_exist=True)

    publish_pair(
        identity_dir=identity_dir,
        promotion_state_path=state_path,
        registry="ghcr.io",
        namespace="sjarmak/codeprobe",
        repository="sjarmak/codeprobe",
        ref="refs/tags/v1.2.3",
        source_sha=SHA,
        cert_identity=CERT_IDENTITY,
        output_dir=tmp_path / "pair",
        runner=runner,
    )

    commands = [call[0] for call in runner.calls]
    first_sign = next(index for index, command in enumerate(commands) if command[:2] == ["cosign", "sign-blob"])
    first_push = next(index for index, command in enumerate(commands) if command[:2] == ["oras", "push"])
    inspect_indices = [
        index
        for index, command in enumerate(commands)
        if command[:4] == ["docker", "buildx", "imagetools", "inspect"]
    ]
    pair_resolve_indices = [
        index
        for index, command in enumerate(commands)
        if command[:2] == ["oras", "resolve"]
        and "codeprobe-release-pair" in command[-1]
    ]
    assert inspect_indices
    assert max(inspect_indices) < first_sign
    assert pair_resolve_indices[0] < first_sign
    assert first_sign < pair_resolve_indices[1] < first_push
    assert first_push < pair_resolve_indices[2]
    ref = json.loads((tmp_path / "pair" / "release-pair-ref.json").read_text())
    assert ref["digest"] == PAIR_DIGEST
    assert ref["digest_ref"] == (
        "ghcr.io/sjarmak/codeprobe/codeprobe-release-pair@" + PAIR_DIGEST
    )
    assert all(timeout in {COMMAND_TIMEOUT_SECONDS} for _, timeout in runner.calls)


def test_publish_pair_existing_authority_fails_when_tag_moves(
    tmp_path: Path,
) -> None:
    identity_dir, state_path = _write_identities_and_state(tmp_path)
    runner = RecordingRunner(
        pair=_pair(), version_tags_exist=True, moved_pair_tag=True
    )

    with pytest.raises(OciReleaseError, match="changed during verification"):
        publish_pair(
            identity_dir=identity_dir,
            promotion_state_path=state_path,
            registry="ghcr.io",
            namespace="sjarmak/codeprobe",
            repository="sjarmak/codeprobe",
            ref="refs/tags/v1.2.3",
            source_sha=SHA,
            cert_identity=CERT_IDENTITY,
            output_dir=tmp_path / "pair",
            runner=runner,
        )


def test_promote_tags_validates_identity_before_mutating_tags(tmp_path: Path) -> None:
    identity_dir = _write_identities(tmp_path)
    runner = RecordingRunner(pair=None, version_tags_exist=False)

    promote_tags(
        identity_dir=identity_dir,
        state_path=tmp_path / "promotion-state.json",
        registry="ghcr.io",
        namespace="sjarmak/codeprobe",
        version="1.2.3",
        source_sha=SHA,
        runner=runner,
    )

    commands = [call[0] for call in runner.calls]
    create_indices = [
        index
        for index, command in enumerate(commands)
        if command[:4] == ["docker", "buildx", "imagetools", "create"]
    ]
    assert len(create_indices) == 2
    first_create = create_indices[0]
    assert any(command[:2] == ["oras", "resolve"] for command in commands[:first_create])


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("tag_ref", "registry.example/other/codeprobe-agent:1.2.3", "official tag"),
        ("source_sha", "b" * 40, "source contract"),
        ("runtime_override_env", "CODEPROBE_OTHER_IMAGE", "runtime override"),
    ],
)
def test_promote_tags_rejects_untrusted_identity_before_create(
    tmp_path: Path, field: str, value: str, match: str
) -> None:
    identity_dir = _write_identities(tmp_path)
    identity_path = identity_dir / "codeprobe-agent.json"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    identity[field] = value
    identity_path.write_text(json.dumps(identity), encoding="utf-8")
    runner = RecordingRunner(pair=None, version_tags_exist=False)

    with pytest.raises(OciReleaseError, match=match):
        promote_tags(
            identity_dir=identity_dir,
            state_path=tmp_path / "promotion-state.json",
            registry="ghcr.io",
            namespace="sjarmak/codeprobe",
            version="1.2.3",
            source_sha=SHA,
            runner=runner,
        )

    commands = [call[0] for call in runner.calls]
    assert not any(command[:4] == ["docker", "buildx", "imagetools", "create"] for command in commands)
    assert not (tmp_path / "promotion-state.json").exists()


def test_promote_tags_rejects_candidate_digest_mismatch_before_create(
    tmp_path: Path,
) -> None:
    identity_dir = _write_identities(tmp_path)
    identity_path = identity_dir / "codeprobe-agent.json"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    identity["digest"] = "sha256:" + "3" * 64
    identity["digest_ref"] = (
        "ghcr.io/sjarmak/codeprobe/codeprobe-agent@sha256:" + "3" * 64
    )
    identity_path.write_text(json.dumps(identity), encoding="utf-8")
    runner = RecordingRunner(pair=None, version_tags_exist=False)

    with pytest.raises(OciReleaseError, match="candidate digest mismatch"):
        promote_tags(
            identity_dir=identity_dir,
            state_path=tmp_path / "promotion-state.json",
            registry="ghcr.io",
            namespace="sjarmak/codeprobe",
            version="1.2.3",
            source_sha=SHA,
            runner=runner,
        )

    commands = [call[0] for call in runner.calls]
    assert not any(command[:4] == ["docker", "buildx", "imagetools", "create"] for command in commands)
    assert not (tmp_path / "promotion-state.json").exists()


def test_publish_pair_rejects_invalid_promotion_state_before_signing(
    tmp_path: Path,
) -> None:
    identity_dir, state_path = _write_identities_and_state(tmp_path)
    state_path.write_text("{}", encoding="utf-8")
    runner = RecordingRunner(pair=None, version_tags_exist=True)

    with pytest.raises(OciReleaseError, match="promotion state"):
        publish_pair(
            identity_dir=identity_dir,
            promotion_state_path=state_path,
            registry="ghcr.io",
            namespace="sjarmak/codeprobe",
            repository="sjarmak/codeprobe",
            ref="refs/tags/v1.2.3",
            source_sha=SHA,
            cert_identity=CERT_IDENTITY,
            output_dir=tmp_path / "pair",
            runner=runner,
        )

    assert not any(call[0][:2] == ["cosign", "sign-blob"] for call in runner.calls)


def test_publish_pair_rejects_identity_relationship_mismatch(tmp_path: Path) -> None:
    identity_dir, state_path = _write_identities_and_state(tmp_path)
    identity_path = identity_dir / "codeprobe-agent.json"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    identity["tag_ref"] = "ghcr.io/sjarmak/codeprobe/codeprobe-agent-extra:1.2.3"
    identity_path.write_text(json.dumps(identity), encoding="utf-8")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["promoted"][0]["tag_ref"] = identity["tag_ref"]
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(OciReleaseError, match="official tag ref"):
        publish_pair(
            identity_dir=identity_dir,
            promotion_state_path=state_path,
            registry="ghcr.io",
            namespace="sjarmak/codeprobe",
            repository="sjarmak/codeprobe",
            ref="refs/tags/v1.2.3",
            source_sha=SHA,
            cert_identity=CERT_IDENTITY,
            output_dir=tmp_path / "pair",
            runner=RecordingRunner(pair=None, version_tags_exist=True),
        )


def test_release_command_launch_errors_are_controlled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def launch_error(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError("cosign")

    monkeypatch.setattr(oci_release.subprocess, "run", launch_error)

    with pytest.raises(OciReleaseError, match="failed to launch"):
        _run_text_command(["cosign", "verify"], COMMAND_TIMEOUT_SECONDS)


def test_release_json_write_errors_are_controlled(tmp_path: Path) -> None:
    output_path = tmp_path / "not-a-directory"
    output_path.write_text("blocks mkdir", encoding="utf-8")

    with pytest.raises(OciReleaseError) as exc_info:
        oci_release._write_json(output_path / "out.json", {"secret": "value"})

    message = str(exc_info.value)
    assert "could not write JSON output" in message
    assert str(output_path) not in message
    assert "secret" not in message


def test_release_json_write_preserves_existing_output_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_path = tmp_path / "release-pair.json"
    output_path.write_text("trusted\n", encoding="utf-8")

    def replace_error(*args: object, **kwargs: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(oci_release.os, "replace", replace_error)

    with pytest.raises(OciReleaseError, match="could not write JSON output"):
        oci_release._write_json(output_path, {"replacement": True})

    assert output_path.read_text(encoding="utf-8") == "trusted\n"
    assert list(tmp_path.glob(".release-pair.json.*.tmp")) == []


def test_release_github_output_write_errors_are_controlled(tmp_path: Path) -> None:
    output_path = tmp_path / "missing" / "github-output"

    with pytest.raises(OciReleaseError) as exc_info:
        oci_release._write_github_output(str(output_path), "secret", "value")

    message = str(exc_info.value)
    assert "could not write GitHub output" in message
    assert str(output_path) not in message
    assert "secret" not in message
