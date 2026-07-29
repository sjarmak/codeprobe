"""Tests for OCI workflow input validation helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from codeprobe.sandbox import oci_workflow_inputs
from codeprobe.sandbox.oci_workflow_inputs import (
    WorkflowInputError,
    image_refs,
    main,
    resolve_credentials,
)

BASE_ENV = {
    "RELEASE_SHA": "a" * 40,
    "GITHUB_RUN_ID": "123",
    "GITHUB_RUN_ATTEMPT": "2",
}


def test_credentials_cli_has_no_secret_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    parser = oci_workflow_inputs._build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["credentials", "--help"])

    help_text = capsys.readouterr().out
    assert "--password" not in help_text
    assert "--username" not in help_text


def test_image_refs_validate_release_and_run_metadata() -> None:
    values = image_refs(
        "ghcr.io",
        "sjarmak/codeprobe",
        "codeprobe-agent",
        "1.2.3",
        BASE_ENV,
    )

    assert values == {
        "name": "ghcr.io/sjarmak/codeprobe/codeprobe-agent",
        "version_ref": "ghcr.io/sjarmak/codeprobe/codeprobe-agent:1.2.3",
        "candidate_ref": (
            "ghcr.io/sjarmak/codeprobe/codeprobe-agent:"
            "1.2.3-123-2-aaaaaaaaaaaa"
        ),
    }


@pytest.mark.parametrize(
    "env",
    [
        {},
        {**BASE_ENV, "RELEASE_SHA": "A" * 40},
        {**BASE_ENV, "GITHUB_RUN_ID": "run-123"},
        {**BASE_ENV, "GITHUB_RUN_ATTEMPT": "2\ninject"},
    ],
)
def test_image_refs_reject_malformed_env_without_values(
    env: dict[str, str],
) -> None:
    with pytest.raises(WorkflowInputError) as exc_info:
        image_refs("ghcr.io", "sjarmak/codeprobe", "codeprobe-agent", "1.2.3", env)

    message = str(exc_info.value)
    assert "inject" not in message
    assert "AAAAAAAA" not in message


def test_resolve_credentials_uses_ghcr_fallback_from_env() -> None:
    values = resolve_credentials(
        "ghcr.io",
        "sjarmak/codeprobe",
        {"GITHUB_ACTOR": "actor", "DEFAULT_GITHUB_TOKEN": "token"},
    )

    assert values == {"username": "actor", "password": "token"}


def test_credentials_reject_crlf_before_mask_or_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_path = tmp_path / "out"
    monkeypatch.setenv("REGISTRY_USERNAME", "actor")
    monkeypatch.setenv("REGISTRY_PASSWORD", "secret\ninjected")

    rc = main(
        [
            "credentials",
            "--registry",
            "registry.example",
            "--namespace",
            "sjarmak/codeprobe",
            "--github-output",
            str(output_path),
        ]
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "secret" not in captured.err
    assert not output_path.exists()


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("actor\tadmin", "secret"),
        ("actor", "secret\x1b[2J"),
        ("actor", "secret\x00suffix"),
        ("actor\x85admin", "secret"),
    ],
)
def test_credentials_reject_all_control_characters(
    username: str, password: str
) -> None:
    with pytest.raises(WorkflowInputError, match="control characters") as exc_info:
        resolve_credentials(
            "registry.example",
            "sjarmak/codeprobe",
            {"REGISTRY_USERNAME": username, "REGISTRY_PASSWORD": password},
        )

    assert username not in str(exc_info.value)
    assert password not in str(exc_info.value)


def test_credentials_mask_password_before_output_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_path = tmp_path / "out"
    monkeypatch.setenv("REGISTRY_USERNAME", "actor")
    monkeypatch.setenv("REGISTRY_PASSWORD", "secret")

    rc = main(
        [
            "credentials",
            "--registry",
            "registry.example",
            "--namespace",
            "sjarmak/codeprobe",
            "--github-output",
            str(output_path),
        ]
    )

    assert rc == 0
    assert capsys.readouterr().out.splitlines() == ["::add-mask::secret"]
    assert output_path.read_text(encoding="utf-8").splitlines() == [
        "username=actor",
        "password=secret",
    ]


def test_credentials_mask_escapes_percent_command_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_path = tmp_path / "out"
    monkeypatch.setenv("REGISTRY_USERNAME", "actor")
    monkeypatch.setenv("REGISTRY_PASSWORD", "secret%0Avalue")

    rc = main(
        [
            "credentials",
            "--registry",
            "registry.example",
            "--namespace",
            "sjarmak/codeprobe",
            "--github-output",
            str(output_path),
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.splitlines() == ["::add-mask::secret%250Avalue"]
    assert captured.err == ""


def test_output_errors_are_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("REGISTRY_USERNAME", "actor")
    monkeypatch.setenv("REGISTRY_PASSWORD", "secret")

    rc = main(
        [
            "credentials",
            "--registry",
            "registry.example",
            "--namespace",
            "sjarmak/codeprobe",
            "--github-output",
            str(tmp_path / "missing" / "out"),
        ]
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert "workflow input validation failed" in captured.err
    assert "secret" not in captured.err


def test_parser_errors_are_sanitized(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["credentials", "--password", "secret", "--registry", "ghcr.io"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "workflow arguments are invalid" in captured.err
    assert "secret" not in captured.err


@pytest.mark.parametrize("registry", ["acme", "bad\nregistry"])
def test_registry_validation_rejects_unqualified_or_multiline(
    registry: str,
) -> None:
    with pytest.raises(WorkflowInputError):
        resolve_credentials(
            registry,
            "sjarmak/codeprobe",
            {"REGISTRY_USERNAME": "actor", "REGISTRY_PASSWORD": "secret"},
        )


@pytest.mark.parametrize(
    ("registry", "namespace"),
    [
        ("[::1", "sjarmak/codeprobe"),
        ("registry:garbage", "sjarmak/codeprobe"),
        ("registry.example", "team name"),
    ],
)
def test_registry_validation_rejects_structurally_invalid_refs(
    registry: str, namespace: str
) -> None:
    with pytest.raises(WorkflowInputError, match="registry or namespace"):
        image_refs(registry, namespace, "codeprobe-agent", "1.2.3", BASE_ENV)


def test_namespace_validation_rejects_multiline() -> None:
    with pytest.raises(WorkflowInputError):
        image_refs(
            "ghcr.io",
            "sjarmak/codeprobe\ninject",
            "codeprobe-agent",
            "1.2.3",
            BASE_ENV,
        )


@pytest.mark.parametrize(
    "env",
    [
        {"REGISTRY_USERNAME": "custom", "DEFAULT_GITHUB_TOKEN": "token"},
        {"REGISTRY_PASSWORD": "custom", "GITHUB_ACTOR": "actor"},
    ],
)
def test_ghcr_credentials_reject_partial_custom_pair(env: dict[str, str]) -> None:
    with pytest.raises(WorkflowInputError, match="incomplete"):
        resolve_credentials("ghcr.io", "sjarmak/codeprobe", env)
