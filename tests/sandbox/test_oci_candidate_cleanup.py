"""Tests for failure-only OCI candidate cleanup."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Final

import pytest

from codeprobe.sandbox import oci_candidate_cleanup
from codeprobe.sandbox.oci_candidate_cleanup import (
    COMMAND_TIMEOUT_SECONDS,
    CandidateCleanupError,
    CandidateCommandError,
    CleanupMetadata,
    _run_text_command,
    cleanup_candidate,
)

SHA: Final[str] = "a" * 40
CANDIDATE_TAG: Final[str] = f"1.2.3-1-1-{SHA[:12]}"
CANDIDATE_REF: Final[str] = f"ghcr.io/sjarmak/codeprobe/codeprobe-agent:{CANDIDATE_TAG}"
BUILD_DIGEST: Final[str] = "sha256:" + "1" * 64
OTHER_DIGEST: Final[str] = "sha256:" + "2" * 64


class CleanupRunner:
    def __init__(self, resolves: list[str | None], tags: list[str] | None = None) -> None:
        self.resolves = resolves
        self.tags = [CANDIDATE_TAG] if tags is None else tags
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, command: list[str], timeout: float) -> str:
        self.calls.append((command, timeout))
        if command[:2] == ["oras", "resolve"]:
            result = self.resolves.pop(0)
            if result is None:
                raise CandidateCommandError("oras", 1, "manifest unknown")
            return result + "\n"
        if command[:3] == ["oras", "repo", "tags"]:
            return "\n".join(self.tags) + "\n"
        return ""


class CleanupFailureRunner:
    def __init__(self, *, fail_second_resolve: bool = False) -> None:
        self.fail_second_resolve = fail_second_resolve
        self.resolve_count = 0
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, command: list[str], timeout: float) -> str:
        self.calls.append((command, timeout))
        if command[:2] == ["oras", "resolve"]:
            self.resolve_count += 1
            if self.fail_second_resolve and self.resolve_count == 2:
                raise CandidateCommandError("oras", 2, "permission denied token=secret")
            return BUILD_DIGEST + "\n"
        if command[:3] == ["oras", "repo", "tags"]:
            return CANDIDATE_TAG + "\n"
        return ""


def _metadata(build_digest: str = BUILD_DIGEST) -> CleanupMetadata:
    digest_ref = (
        f"ghcr.io/sjarmak/codeprobe/codeprobe-agent@{build_digest}"
        if build_digest
        else ""
    )
    return CleanupMetadata(
        candidate_ref=CANDIDATE_REF,
        image="codeprobe-agent",
        version="1.2.3",
        source_sha=SHA,
        build_digest=build_digest,
        digest_ref=digest_ref,
        repository="sjarmak/codeprobe",
        ref="refs/tags/v1.2.3",
        run_id="1",
        run_attempt="1",
    )


def test_cleanup_candidate_quarantines_without_non_atomic_delete(tmp_path: Path) -> None:
    runner = CleanupRunner([BUILD_DIGEST, BUILD_DIGEST])

    assert cleanup_candidate(_metadata(), tmp_path, runner) is True

    commands = [call[0] for call in runner.calls]
    assert commands == [
        ["oras", "resolve", CANDIDATE_REF],
        ["oras", "repo", "tags", "ghcr.io/sjarmak/codeprobe/codeprobe-agent"],
        ["oras", "resolve", CANDIDATE_REF],
    ]
    assert not any(command[:3] == ["oras", "manifest", "delete"] for command in commands)
    quarantine = json.loads((tmp_path / "candidate-quarantine.json").read_text())
    assert quarantine["reason"] == "candidate-deletion-not-atomic"


def test_cleanup_candidate_quarantines_initial_resolve_error(tmp_path: Path) -> None:
    def fail_resolve(command: list[str], timeout: float) -> str:
        raise CandidateCommandError("oras", 2, "permission denied token=secret")

    assert cleanup_candidate(_metadata(), tmp_path, fail_resolve) is True

    quarantine = json.loads((tmp_path / "candidate-quarantine.json").read_text())
    assert quarantine["reason"] == "candidate-resolve-failed"
    assert "secret" not in json.dumps(quarantine)


def test_cleanup_candidate_quarantines_pre_delete_resolve_error(
    tmp_path: Path,
) -> None:
    runner = CleanupFailureRunner(fail_second_resolve=True)

    assert cleanup_candidate(_metadata(), tmp_path, runner) is True

    quarantine = json.loads((tmp_path / "candidate-quarantine.json").read_text())
    assert quarantine["reason"] == "candidate-resolve-failed"
    assert runner.resolve_count == 2


def test_cleanup_candidate_quarantines_digest_mismatch(tmp_path: Path) -> None:
    runner = CleanupRunner([OTHER_DIGEST])

    assert cleanup_candidate(_metadata(), tmp_path, runner) is True

    commands = [call[0] for call in runner.calls]
    assert commands == [["oras", "resolve", CANDIDATE_REF]]
    quarantine = json.loads((tmp_path / "candidate-quarantine.json").read_text())
    assert quarantine["reason"] == "candidate-digest-mismatch"
    assert quarantine["observed_digest"] == OTHER_DIGEST


def test_cleanup_candidate_quarantines_candidate_move_before_delete(
    tmp_path: Path,
) -> None:
    runner = CleanupRunner([BUILD_DIGEST, OTHER_DIGEST])

    assert cleanup_candidate(_metadata(), tmp_path, runner) is True

    commands = [call[0] for call in runner.calls]
    assert not any(command[:3] == ["oras", "manifest", "delete"] for command in commands)
    quarantine = json.loads((tmp_path / "candidate-quarantine.json").read_text())
    assert quarantine["reason"] == "candidate-digest-changed"
    assert quarantine["observed_digest"] == BUILD_DIGEST
    assert quarantine["current_digest"] == OTHER_DIGEST


def test_cleanup_candidate_quarantines_shared_digest_tag(tmp_path: Path) -> None:
    runner = CleanupRunner([BUILD_DIGEST, BUILD_DIGEST], tags=[CANDIDATE_TAG, "1.2.3"])

    assert cleanup_candidate(_metadata(), tmp_path, runner) is True

    commands = [call[0] for call in runner.calls]
    assert ["oras", "manifest", "delete", "--force", CANDIDATE_REF] not in commands
    quarantine = json.loads((tmp_path / "candidate-quarantine.json").read_text())
    assert quarantine["reason"] == "candidate-digest-shared"
    assert quarantine["shared_ref"] == "ghcr.io/sjarmak/codeprobe/codeprobe-agent:1.2.3"


def test_cleanup_candidate_quarantines_when_other_tags_do_not_share_digest(
    tmp_path: Path,
) -> None:
    runner = CleanupRunner(
        [BUILD_DIGEST, OTHER_DIGEST, BUILD_DIGEST],
        tags=[CANDIDATE_TAG, "older"],
    )

    assert cleanup_candidate(_metadata(), tmp_path, runner) is True

    commands = [call[0] for call in runner.calls]
    assert ["oras", "resolve", "ghcr.io/sjarmak/codeprobe/codeprobe-agent:older"] in commands
    assert not any(command[:3] == ["oras", "manifest", "delete"] for command in commands)
    quarantine = json.loads((tmp_path / "candidate-quarantine.json").read_text())
    assert quarantine["reason"] == "candidate-deletion-not-atomic"


def test_cleanup_candidate_quarantines_tag_list_failure(tmp_path: Path) -> None:
    def fail_tags(command: list[str], timeout: float) -> str:
        if command[:2] == ["oras", "resolve"]:
            return BUILD_DIGEST + "\n"
        raise CandidateCommandError("oras", 2, "credentials not found token=secret")

    assert cleanup_candidate(_metadata(), tmp_path, fail_tags) is True

    quarantine = json.loads((tmp_path / "candidate-quarantine.json").read_text())
    assert quarantine["reason"] == "candidate-tag-scan-failed"
    assert "secret" not in json.dumps(quarantine)


def test_cleanup_candidate_quarantines_other_tag_resolve_failure(
    tmp_path: Path,
) -> None:
    def fail_other_resolve(command: list[str], timeout: float) -> str:
        if command[:3] == ["oras", "repo", "tags"]:
            return f"{CANDIDATE_TAG}\nother\n"
        if command[-1].endswith(":other"):
            raise CandidateCommandError("oras", 2, "credentials not found token=secret")
        return BUILD_DIGEST + "\n"

    assert cleanup_candidate(_metadata(), tmp_path, fail_other_resolve) is True

    quarantine = json.loads((tmp_path / "candidate-quarantine.json").read_text())
    assert quarantine["reason"] == "candidate-tag-scan-failed"
    assert "secret" not in json.dumps(quarantine)


def test_cleanup_candidate_does_not_treat_credentials_not_found_as_absent(
    tmp_path: Path,
) -> None:
    def fail_resolve(command: list[str], timeout: float) -> str:
        raise CandidateCommandError("oras", 2, "credentials not found")

    assert cleanup_candidate(_metadata(), tmp_path, fail_resolve) is True
    quarantine = json.loads((tmp_path / "candidate-quarantine.json").read_text())
    assert quarantine["reason"] == "candidate-resolve-failed"


def test_cleanup_candidate_does_not_treat_auth_endpoint_404_as_absent(
    tmp_path: Path,
) -> None:
    def fail_resolve(command: list[str], timeout: float) -> str:
        raise CandidateCommandError("oras", 2, "authentication endpoint returned 404")

    assert cleanup_candidate(_metadata(), tmp_path, fail_resolve) is True
    quarantine = json.loads((tmp_path / "candidate-quarantine.json").read_text())
    assert quarantine["reason"] == "candidate-resolve-failed"


def test_cleanup_candidate_does_not_treat_auth_error_with_absence_text_as_absent(
    tmp_path: Path,
) -> None:
    def fail_resolve(command: list[str], timeout: float) -> str:
        raise CandidateCommandError(
            "oras", 2, "UNAUTHORIZED: token rejected; manifest unknown is not proof"
        )

    assert cleanup_candidate(_metadata(), tmp_path, fail_resolve) is True
    quarantine = json.loads((tmp_path / "candidate-quarantine.json").read_text())
    assert quarantine["reason"] == "candidate-resolve-failed"


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
def test_cleanup_candidate_rejects_prefixed_false_absence(
    tmp_path: Path, stderr: str
) -> None:
    def fail_resolve(command: list[str], timeout: float) -> str:
        raise CandidateCommandError("oras", 2, stderr)

    assert cleanup_candidate(_metadata(), tmp_path, fail_resolve) is True
    quarantine = json.loads((tmp_path / "candidate-quarantine.json").read_text())
    assert quarantine["reason"] == "candidate-resolve-failed"


def test_cleanup_candidate_accepts_exact_oras_not_found(tmp_path: Path) -> None:
    def fail_resolve(command: list[str], timeout: float) -> str:
        raise CandidateCommandError(
            "oras",
            1,
            (
                "Error response from registry: failed to resolve digest: "
                f"{CANDIDATE_REF}: not found"
            ),
        )

    assert cleanup_candidate(_metadata(), tmp_path, fail_resolve) is False
    assert not (tmp_path / "candidate-quarantine.json").exists()


@pytest.mark.parametrize(
    "updates",
    [
        {"image": "other-image"},
        {"source_sha": "A" * 40},
        {"run_id": "run-1"},
        {"run_attempt": "attempt-1"},
        {"candidate_ref": "ghcr.io/sjarmak/codeprobe/codeprobe-agent:wrong"},
        {"candidate_ref": f"ghcr.io/sjarmak/codeprobe/other:{CANDIDATE_TAG}"},
        {"build_digest": "sha256:abc", "digest_ref": "bad"},
        {"digest_ref": "ghcr.io/sjarmak/codeprobe/codeprobe-agent@sha256:" + "2" * 64},
    ],
)
def test_cleanup_candidate_rejects_malformed_metadata_before_registry_calls(
    tmp_path: Path, updates: dict[str, str]
) -> None:
    metadata = _metadata()
    metadata = CleanupMetadata(
        **{**metadata.__dict__, **updates},
    )
    calls: list[list[str]] = []

    def runner(command: list[str], timeout: float) -> str:
        calls.append(command)
        return BUILD_DIGEST + "\n"

    with pytest.raises(CandidateCleanupError, match="cleanup metadata|candidate"):
        cleanup_candidate(metadata, tmp_path, runner)

    assert calls == []


def test_cleanup_candidate_quarantines_when_build_digest_is_missing(tmp_path: Path) -> None:
    runner = CleanupRunner([BUILD_DIGEST, BUILD_DIGEST])

    assert cleanup_candidate(_metadata(build_digest=""), tmp_path, runner) is True

    quarantine = json.loads((tmp_path / "candidate-quarantine.json").read_text())
    assert quarantine["reason"] == "candidate-deletion-not-atomic"


def test_cleanup_command_has_bounded_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["timeout"] == COMMAND_TIMEOUT_SECONDS
        raise subprocess.TimeoutExpired(cmd=["oras"], timeout=COMMAND_TIMEOUT_SECONDS)

    monkeypatch.setattr(subprocess, "run", timeout_run)

    with pytest.raises(CandidateCleanupError, match="timed out"):
        _run_text_command(["oras", "resolve", CANDIDATE_REF], COMMAND_TIMEOUT_SECONDS)


def test_cleanup_candidate_quarantines_missing_oras(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def launch_error(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("oras")

    monkeypatch.setattr(subprocess, "run", launch_error)

    assert cleanup_candidate(_metadata(), tmp_path) is True
    quarantine = json.loads((tmp_path / "candidate-quarantine.json").read_text())
    assert quarantine["reason"] == "candidate-resolve-failed"


def test_cleanup_candidate_reports_quarantine_write_failure_without_values(
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "not-a-directory"
    output_path.write_text("blocks mkdir", encoding="utf-8")

    with pytest.raises(CandidateCleanupError) as exc_info:
        cleanup_candidate(_metadata(), output_path, CleanupRunner([OTHER_DIGEST]))

    message = str(exc_info.value)
    assert "candidate quarantine evidence" in message
    assert str(output_path) not in message
    assert CANDIDATE_REF not in message


def test_cleanup_candidate_preserves_existing_quarantine_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_path = tmp_path / "candidate-quarantine.json"
    output_path.write_text("trusted\n", encoding="utf-8")

    def replace_error(*args: object, **kwargs: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(oci_candidate_cleanup.os, "replace", replace_error)

    with pytest.raises(
        CandidateCleanupError, match="could not persist candidate quarantine evidence"
    ):
        cleanup_candidate(_metadata(), tmp_path, CleanupRunner([OTHER_DIGEST]))

    assert output_path.read_text(encoding="utf-8") == "trusted\n"
    assert list(tmp_path.glob(".candidate-quarantine.json.*.tmp")) == []
