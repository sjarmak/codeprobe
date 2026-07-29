from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

import codeprobe.cli.bootstrap_cmd as bootstrap_module
from codeprobe.cli import main
from codeprobe.cli.bootstrap_cmd import bootstrap
from codeprobe.cli.errors import DiagnosticError
from codeprobe.sandbox.image_bootstrap import (
    BootstrapResult,
    ImageBootstrapError,
)
from codeprobe.sandbox.image_config import PreparedImage, PreparedImages

_AGENT_DIGEST = "sha256:" + "a" * 64
_SCORING_DIGEST = "sha256:" + "b" * 64
_AGENT_LOCAL_ID = "sha256:" + "c" * 64
_SCORING_LOCAL_ID = "sha256:" + "d" * 64
_AGENT_REF = "private.example/team/codeprobe-agent:0.13.0"
_SCORING_REF = "private.example/team/codeprobe-scoring:0.13.0"


def _result(path: Path) -> BootstrapResult:
    prepared = PreparedImages(
        engine="docker",
        agent=PreparedImage(
            _AGENT_REF,
            f"{_AGENT_REF}@{_AGENT_DIGEST}",
            _AGENT_DIGEST,
            _AGENT_LOCAL_ID,
        ),
        scoring=PreparedImage(
            _SCORING_REF,
            f"{_SCORING_REF}@{_SCORING_DIGEST}",
            _SCORING_DIGEST,
            _SCORING_LOCAL_ID,
        ),
    )
    return BootstrapResult(prepared=prepared, config_path=path)


def test_root_cli_registers_noninteractive_bootstrap_command() -> None:
    result = CliRunner().invoke(main, ["bootstrap", "--help"])

    assert result.exit_code == 0, result.output
    assert "--agent-image" in result.output
    assert "--scoring-image" in result.output
    assert "--agent-digest" in result.output
    assert "--scoring-digest" in result.output
    assert "--agent-archive" in result.output
    assert "--scoring-archive" in result.output
    assert "--engine" in result.output
    assert "--config" not in result.output
    assert "Dockerfile" not in result.output


def test_bootstrap_passes_private_registry_inputs_and_reports_local_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "container-images.json"
    captured: dict[str, object] = {}
    monkeypatch.setenv("CODEPROBE_CONTAINER_CONFIG", str(config_path))

    def fake_prepare(**kwargs: object) -> BootstrapResult:
        captured.update(kwargs)
        return _result(config_path)

    monkeypatch.setattr(bootstrap_module, "prepare_images", fake_prepare)

    result = CliRunner().invoke(
        bootstrap,
        [
            "--engine",
            "docker",
            "--agent-image",
            _AGENT_REF,
            "--agent-digest",
            _AGENT_DIGEST,
            "--scoring-image",
            _SCORING_REF,
            "--scoring-digest",
            _SCORING_DIGEST,
            "--no-json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["engine"] == "docker"
    assert captured["agent_reference"] == _AGENT_REF
    assert captured["scoring_reference"] == _SCORING_REF
    assert captured["agent_digest"] == _AGENT_DIGEST
    assert captured["scoring_digest"] == _SCORING_DIGEST
    assert captured["config_path"] is None
    assert _AGENT_LOCAL_ID in result.output
    assert _SCORING_LOCAL_ID in result.output
    assert str(config_path) in result.output
    assert "ready" in result.output.lower()


def test_bootstrap_resolves_existing_source_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        bootstrap_module.container_runner,
        "agent_source_image_reference",
        lambda: _AGENT_REF,
    )
    monkeypatch.setattr(
        bootstrap_module.container_runner,
        "scoring_source_image_reference",
        lambda: _SCORING_REF,
    )

    def fake_prepare(**kwargs: object) -> BootstrapResult:
        captured.update(kwargs)
        return _result(tmp_path / "config.json")

    monkeypatch.setattr(bootstrap_module, "prepare_images", fake_prepare)

    result = CliRunner().invoke(
        bootstrap,
        [
            "--agent-digest",
            _AGENT_DIGEST,
            "--scoring-digest",
            _SCORING_DIGEST,
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["agent_reference"] == _AGENT_REF
    assert captured["scoring_reference"] == _SCORING_REF


def test_bootstrap_json_mode_emits_one_success_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        bootstrap_module,
        "prepare_images",
        lambda **_kwargs: _result(tmp_path / "config.json"),
    )

    result = CliRunner().invoke(
        bootstrap,
        [
            "--agent-image",
            f"{_AGENT_REF}@{_AGENT_DIGEST}",
            "--scoring-image",
            f"{_SCORING_REF}@{_SCORING_DIGEST}",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["record_type"] == "envelope"
    assert payload["ok"] is True
    assert payload["command"] == "bootstrap"
    assert payload["data"]["engine"] == "docker"
    assert payload["data"]["agent"]["local_id"] == _AGENT_LOCAL_ID
    assert payload["data"]["scoring"]["digest"] == _SCORING_DIGEST


def test_bootstrap_failure_is_structured_and_prescriptive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(**_kwargs: object) -> BootstrapResult:
        raise ImageBootstrapError("No container engine found; install docker or podman")

    monkeypatch.setattr(bootstrap_module, "prepare_images", fail)

    result = CliRunner().invoke(
        bootstrap,
        [
            "--agent-image",
            f"{_AGENT_REF}@{_AGENT_DIGEST}",
            "--scoring-image",
            f"{_SCORING_REF}@{_SCORING_DIGEST}",
        ],
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, DiagnosticError)
    assert result.exception.code == "CONTAINER_BOOTSTRAP_FAILED"
    assert "docker or podman" in result.exception.message
    assert result.exception.diagnose_cmd == "codeprobe bootstrap --help"


def test_bootstrap_rejects_partial_archive_pair_before_implementation(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "agent.tar"
    archive.write_bytes(b"archive")

    result = CliRunner().invoke(
        bootstrap,
        [
            "--agent-image",
            f"{_AGENT_REF}@{_AGENT_DIGEST}",
            "--scoring-image",
            f"{_SCORING_REF}@{_SCORING_DIGEST}",
            "--agent-archive",
            str(archive),
        ],
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, DiagnosticError)
    assert "both" in result.exception.message.lower()


def test_bootstrap_rejects_conflicting_output_flags_before_engine_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_called = False

    def fake_prepare(**_kwargs: object) -> BootstrapResult:
        nonlocal prepare_called
        prepare_called = True
        raise AssertionError("bootstrap work must not start")

    monkeypatch.setattr(bootstrap_module, "prepare_images", fake_prepare)

    result = CliRunner().invoke(
        bootstrap,
        [
            "--agent-image",
            f"{_AGENT_REF}@{_AGENT_DIGEST}",
            "--scoring-image",
            f"{_SCORING_REF}@{_SCORING_DIGEST}",
            "--json",
            "--no-json",
        ],
    )

    assert result.exit_code != 0
    assert not prepare_called
