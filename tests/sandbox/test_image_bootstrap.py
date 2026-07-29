from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from codeprobe.sandbox.image_bootstrap import (
    ImageBootstrapError,
    _run_text_command,
    prepare_images,
)
from codeprobe.sandbox.image_config import load_prepared_images

_AGENT_DIGEST = "sha256:" + "a" * 64
_SCORING_DIGEST = "sha256:" + "b" * 64
_AGENT_LOCAL_ID = "sha256:" + "c" * 64
_SCORING_LOCAL_ID = "sha256:" + "d" * 64
_AGENT_REF = "registry.example/team/codeprobe-agent:0.13.0"
_SCORING_REF = "registry.example/team/codeprobe-scoring:0.13.0"


class RecordingRunner:
    def __init__(self, responses: dict[tuple[str, ...], str]) -> None:
        self.responses = responses
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def __call__(self, command: Sequence[str], timeout: float) -> str:
        key = tuple(command)
        self.calls.append((key, timeout))
        response = self.responses.get(key)
        if response is None:
            raise AssertionError(f"unexpected command: {key!r}")
        return response


def _inspect(local_id: str, digest: str) -> str:
    return json.dumps(
        [
            {
                "Id": local_id,
                "RepoDigests": [f"registry.example/team/image@{digest}"],
                "Digest": digest,
            }
        ]
    )


def _online_responses(engine_path: str) -> dict[tuple[str, ...], str]:
    agent_pinned = f"{_AGENT_REF}@{_AGENT_DIGEST}"
    scoring_pinned = f"{_SCORING_REF}@{_SCORING_DIGEST}"
    return {
        (engine_path, "image", "pull", agent_pinned): "",
        (engine_path, "image", "inspect", agent_pinned): _inspect(_AGENT_LOCAL_ID, _AGENT_DIGEST),
        (engine_path, "image", "pull", scoring_pinned): "",
        (engine_path, "image", "inspect", scoring_pinned): _inspect(_SCORING_LOCAL_ID, _SCORING_DIGEST),
    }


@pytest.mark.parametrize(("engine", "engine_path"), [("docker", "/usr/bin/docker"), ("podman", "/usr/bin/podman")])
def test_online_bootstrap_pulls_verifies_and_persists_both_images(
    tmp_path: Path, engine: str, engine_path: str
) -> None:
    runner = RecordingRunner(_online_responses(engine_path))
    config_path = tmp_path / "container-images.json"

    result = prepare_images(
        engine=engine,
        agent_reference=_AGENT_REF,
        scoring_reference=_SCORING_REF,
        agent_digest=_AGENT_DIGEST,
        scoring_digest=_SCORING_DIGEST,
        config_path=config_path,
        runner=runner,
        which=lambda name: engine_path if name == engine else None,
    )

    assert result.engine == engine
    assert result.agent.local_id == _AGENT_LOCAL_ID
    assert result.scoring.local_id == _SCORING_LOCAL_ID
    assert result.config_path == config_path
    assert load_prepared_images(config_path) == result.prepared
    assert [call[0][1:3] for call in runner.calls] == [
        ("image", "pull"),
        ("image", "inspect"),
        ("image", "pull"),
        ("image", "inspect"),
    ]


def test_digest_pinned_references_do_not_need_separate_digest_flags(
    tmp_path: Path,
) -> None:
    engine_path = "/usr/bin/docker"
    agent_pinned = f"registry.example/team/codeprobe-agent@{_AGENT_DIGEST}"
    scoring_pinned = f"registry.example/team/codeprobe-scoring@{_SCORING_DIGEST}"
    responses = {
        (engine_path, "image", "pull", agent_pinned): "",
        (engine_path, "image", "inspect", agent_pinned): _inspect(_AGENT_LOCAL_ID, _AGENT_DIGEST),
        (engine_path, "image", "pull", scoring_pinned): "",
        (engine_path, "image", "inspect", scoring_pinned): _inspect(_SCORING_LOCAL_ID, _SCORING_DIGEST),
    }

    result = prepare_images(
        engine="docker",
        agent_reference=agent_pinned,
        scoring_reference=scoring_pinned,
        config_path=tmp_path / "config.json",
        runner=RecordingRunner(responses),
        which=lambda _name: engine_path,
    )

    assert result.agent.verified_reference == agent_pinned
    assert result.scoring.verified_reference == scoring_pinned


def test_tag_reference_without_expected_digest_fails_before_pull(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner({})

    with pytest.raises(ImageBootstrapError, match="agent.*digest"):
        prepare_images(
            engine="docker",
            agent_reference=_AGENT_REF,
            scoring_reference=_SCORING_REF,
            scoring_digest=_SCORING_DIGEST,
            config_path=tmp_path / "config.json",
            runner=runner,
            which=lambda _name: "/usr/bin/docker",
        )

    assert runner.calls == []


def test_digest_flag_must_match_digest_pinned_reference(tmp_path: Path) -> None:
    runner = RecordingRunner({})

    with pytest.raises(ImageBootstrapError, match="does not match"):
        prepare_images(
            engine="docker",
            agent_reference=f"{_AGENT_REF}@{_AGENT_DIGEST}",
            scoring_reference=f"{_SCORING_REF}@{_SCORING_DIGEST}",
            agent_digest=_SCORING_DIGEST,
            config_path=tmp_path / "config.json",
            runner=runner,
            which=lambda _name: "/usr/bin/docker",
        )

    assert runner.calls == []


def test_missing_requested_engine_fails_prescriptively(tmp_path: Path) -> None:
    with pytest.raises(ImageBootstrapError, match="docker.*PATH"):
        prepare_images(
            engine="docker",
            agent_reference=f"{_AGENT_REF}@{_AGENT_DIGEST}",
            scoring_reference=f"{_SCORING_REF}@{_SCORING_DIGEST}",
            config_path=tmp_path / "config.json",
            runner=RecordingRunner({}),
            which=lambda _name: None,
        )


def test_auto_engine_refuses_when_docker_and_podman_are_missing(
    tmp_path: Path,
) -> None:
    with pytest.raises(ImageBootstrapError, match="docker or podman"):
        prepare_images(
            engine=None,
            agent_reference=f"{_AGENT_REF}@{_AGENT_DIGEST}",
            scoring_reference=f"{_SCORING_REF}@{_SCORING_DIGEST}",
            config_path=tmp_path / "config.json",
            runner=RecordingRunner({}),
            which=lambda _name: None,
        )


def test_local_digest_mismatch_does_not_write_config(tmp_path: Path) -> None:
    engine_path = "/usr/bin/docker"
    responses = _online_responses(engine_path)
    agent_pinned = f"{_AGENT_REF}@{_AGENT_DIGEST}"
    responses[(engine_path, "image", "inspect", agent_pinned)] = _inspect(_AGENT_LOCAL_ID, _SCORING_DIGEST)
    config_path = tmp_path / "config.json"

    with pytest.raises(ImageBootstrapError, match="digest mismatch"):
        prepare_images(
            engine="docker",
            agent_reference=_AGENT_REF,
            scoring_reference=_SCORING_REF,
            agent_digest=_AGENT_DIGEST,
            scoring_digest=_SCORING_DIGEST,
            config_path=config_path,
            runner=RecordingRunner(responses),
            which=lambda _name: engine_path,
        )

    assert not config_path.exists()


def test_offline_archives_are_verified_then_copied_without_pull(
    tmp_path: Path,
) -> None:
    engine_path = "/usr/bin/docker"
    skopeo_path = "/usr/bin/skopeo"
    agent_archive = tmp_path / "agent.tar"
    scoring_archive = tmp_path / "scoring.tar"
    agent_archive.write_bytes(b"agent")
    scoring_archive.write_bytes(b"scoring")
    agent_dest = _AGENT_REF
    scoring_dest = _SCORING_REF
    responses = {
        (
            skopeo_path,
            "inspect",
            "--format",
            "{{.Digest}}",
            f"oci-archive:{agent_archive}",
        ): _AGENT_DIGEST,
        (
            skopeo_path,
            "copy",
            f"oci-archive:{agent_archive}",
            f"docker-daemon:{agent_dest}",
        ): "",
        (engine_path, "image", "inspect", agent_dest): json.dumps([{"Id": _AGENT_LOCAL_ID}]),
        (
            skopeo_path,
            "inspect",
            "--format",
            "{{.Digest}}",
            f"oci-archive:{scoring_archive}",
        ): _SCORING_DIGEST,
        (
            skopeo_path,
            "copy",
            f"oci-archive:{scoring_archive}",
            f"docker-daemon:{scoring_dest}",
        ): "",
        (engine_path, "image", "inspect", scoring_dest): json.dumps([{"Id": _SCORING_LOCAL_ID}]),
    }
    runner = RecordingRunner(responses)

    result = prepare_images(
        engine="docker",
        agent_reference=_AGENT_REF,
        scoring_reference=_SCORING_REF,
        agent_digest=_AGENT_DIGEST,
        scoring_digest=_SCORING_DIGEST,
        agent_archive=agent_archive,
        scoring_archive=scoring_archive,
        config_path=tmp_path / "config.json",
        runner=runner,
        which=lambda name: {
            "docker": engine_path,
            "skopeo": skopeo_path,
        }.get(name),
    )

    assert result.agent.local_id == _AGENT_LOCAL_ID
    assert not any(call[0][1:3] == ("image", "pull") for call in runner.calls)


def test_offline_archives_must_be_supplied_as_a_pair(tmp_path: Path) -> None:
    archive = tmp_path / "agent.tar"
    archive.write_bytes(b"agent")

    with pytest.raises(ImageBootstrapError, match="both.*archives"):
        prepare_images(
            engine="docker",
            agent_reference=f"{_AGENT_REF}@{_AGENT_DIGEST}",
            scoring_reference=f"{_SCORING_REF}@{_SCORING_DIGEST}",
            agent_archive=archive,
            config_path=tmp_path / "config.json",
            runner=RecordingRunner({}),
            which=lambda _name: "/usr/bin/docker",
        )


def test_offline_archive_digest_mismatch_fails_before_copy(
    tmp_path: Path,
) -> None:
    agent_archive = tmp_path / "agent.tar"
    scoring_archive = tmp_path / "scoring.tar"
    agent_archive.write_bytes(b"agent")
    scoring_archive.write_bytes(b"scoring")
    skopeo_path = "/usr/bin/skopeo"
    inspect_command = (
        skopeo_path,
        "inspect",
        "--format",
        "{{.Digest}}",
        f"oci-archive:{agent_archive}",
    )
    runner = RecordingRunner({inspect_command: _SCORING_DIGEST})

    with pytest.raises(ImageBootstrapError, match="archive digest mismatch"):
        prepare_images(
            engine="docker",
            agent_reference=_AGENT_REF,
            scoring_reference=_SCORING_REF,
            agent_digest=_AGENT_DIGEST,
            scoring_digest=_SCORING_DIGEST,
            agent_archive=agent_archive,
            scoring_archive=scoring_archive,
            config_path=tmp_path / "config.json",
            runner=runner,
            which=lambda name: {
                "docker": "/usr/bin/docker",
                "skopeo": skopeo_path,
            }.get(name),
        )

    assert len(runner.calls) == 1


def test_offline_import_rejects_conflicting_engine_digest(
    tmp_path: Path,
) -> None:
    engine_path = "/usr/bin/docker"
    skopeo_path = "/usr/bin/skopeo"
    agent_archive = tmp_path / "agent.tar"
    scoring_archive = tmp_path / "scoring.tar"
    agent_archive.write_bytes(b"agent")
    scoring_archive.write_bytes(b"scoring")
    responses = {
        (
            skopeo_path,
            "inspect",
            "--format",
            "{{.Digest}}",
            f"oci-archive:{agent_archive}",
        ): _AGENT_DIGEST,
        (
            skopeo_path,
            "copy",
            f"oci-archive:{agent_archive}",
            f"docker-daemon:{_AGENT_REF}",
        ): "",
        (engine_path, "image", "inspect", _AGENT_REF): _inspect(_AGENT_LOCAL_ID, _SCORING_DIGEST),
    }
    config_path = tmp_path / "config.json"

    with pytest.raises(ImageBootstrapError, match="digest mismatch"):
        prepare_images(
            engine="docker",
            agent_reference=_AGENT_REF,
            scoring_reference=_SCORING_REF,
            agent_digest=_AGENT_DIGEST,
            scoring_digest=_SCORING_DIGEST,
            agent_archive=agent_archive,
            scoring_archive=scoring_archive,
            config_path=config_path,
            runner=RecordingRunner(responses),
            which=lambda name: {
                "docker": engine_path,
                "skopeo": skopeo_path,
            }.get(name),
        )

    assert not config_path.exists()


def test_command_timeout_is_bounded_and_does_not_expose_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(["docker", "image", "pull"], 1)

    monkeypatch.setattr(subprocess, "run", timeout)

    with pytest.raises(ImageBootstrapError, match="timed out") as exc_info:
        _run_text_command(["docker", "image", "pull", _AGENT_REF], 1)

    assert "secret" not in str(exc_info.value)


def test_command_failure_reports_exit_without_exposing_stderr() -> None:
    command = [
        sys.executable,
        "-c",
        "import sys; print('registry-secret', file=sys.stderr); raise SystemExit(9)",
    ]

    with pytest.raises(ImageBootstrapError, match="failed with exit 9") as exc_info:
        _run_text_command(command, 5)

    assert "registry-secret" not in str(exc_info.value)


def test_command_inherits_proxy_and_private_ca_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.test:8443")
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/example/private-ca.pem")
    command = [
        sys.executable,
        "-c",
        ("import json, os; print(json.dumps([os.environ['HTTPS_PROXY'], os.environ['SSL_CERT_FILE']]))"),
    ]

    observed = json.loads(_run_text_command(command, 5))

    assert observed == [
        "http://proxy.example.test:8443",
        "/etc/example/private-ca.pem",
    ]
    assert os.environ["HTTPS_PROXY"] == "http://proxy.example.test:8443"
