"""End-to-end CLI flow for data-owner preview and bound approval."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from codeprobe.cli import main
from codeprobe.snapshot.evidence_bundle import ARTIFACT_FILENAMES
from tests.snapshot._evidence_helpers import evidence_request


def _write_request(tmp_path: Path) -> Path:
    path = tmp_path / "bundle-request.json"
    path.write_text(json.dumps(evidence_request()))
    return path


def _invoke_preview(runner: CliRunner, request_path: Path) -> Result:
    return runner.invoke(
        main,
        [
            "snapshot",
            "evidence",
            "preview",
            str(request_path),
            "--no-json",
        ],
    )


def _invoke_export(
    runner: CliRunner,
    request_path: Path,
    out: Path,
    approval_digest: str,
) -> Result:
    return runner.invoke(
        main,
        [
            "snapshot",
            "evidence",
            "export",
            str(request_path),
            "--out",
            str(out),
            "--approve",
            approval_digest,
            "--no-json",
        ],
    )


def _export_bundle_with_cli(
    runner: CliRunner,
    tmp_path: Path,
) -> tuple[Path, str]:
    request_path = _write_request(tmp_path)
    out = tmp_path / "approved-bundle"
    preview = _invoke_preview(runner, request_path)
    assert preview.exit_code == 0, preview.output
    approval_digest = json.loads(preview.output)["approval_digest"]
    exported = _invoke_export(runner, request_path, out, approval_digest)
    assert exported.exit_code == 0, exported.output
    return out, approval_digest


def test_evidence_commands_are_discoverable() -> None:
    runner = CliRunner()

    group_help = runner.invoke(main, ["snapshot", "evidence", "--help"])
    preview_help = runner.invoke(main, ["snapshot", "evidence", "preview", "--help"])
    export_help = runner.invoke(main, ["snapshot", "evidence", "export", "--help"])
    validate_help = runner.invoke(
        main,
        ["snapshot", "evidence", "validate", "--help"],
    )

    assert group_help.exit_code == 0, group_help.output
    assert "preview" in group_help.output
    assert "export" in group_help.output
    assert "validate" in group_help.output
    assert preview_help.exit_code == 0, preview_help.output
    assert "REQUEST" in preview_help.output
    assert export_help.exit_code == 0, export_help.output
    assert "--approve" in export_help.output
    assert "--out" in export_help.output
    assert validate_help.exit_code == 0, validate_help.output
    assert "BUNDLE" in validate_help.output
    assert "--expect" in validate_help.output


@pytest.mark.integration
def test_data_owner_previews_then_exports_exact_approved_bundle(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    request_path = _write_request(tmp_path)
    out = tmp_path / "approved-bundle"

    preview_result = _invoke_preview(runner, request_path)
    assert preview_result.exit_code == 0, preview_result.output
    preview_payload = json.loads(preview_result.output)
    assert preview_payload["artifacts"] == list(ARTIFACT_FILENAMES)
    approval_digest = preview_payload["approval_digest"]
    assert preview_payload["approval_required"] is True
    assert not out.exists()

    denied = _invoke_export(
        runner,
        request_path,
        out,
        "sha256:" + ("0" * 64),
    )
    assert denied.exit_code != 0
    assert "approval digest does not match" in denied.output
    assert not out.exists()

    exported = _invoke_export(runner, request_path, out, approval_digest)
    assert exported.exit_code == 0, exported.output
    payload = json.loads(exported.output)
    assert payload == {
        "approval_digest": approval_digest,
        "artifacts": list(ARTIFACT_FILENAMES),
        "out": str(out),
    }
    assert sorted(path.name for path in out.iterdir()) == sorted(ARTIFACT_FILENAMES)


def test_cli_rejects_prohibited_request_without_leaking_value(
    tmp_path: Path,
) -> None:
    request = evidence_request()
    request["support"]["events"][0]["diagnostic"] = "PRIVATE_DATA_SENTINEL"
    request_path = tmp_path / "prohibited.json"
    request_path.write_text(json.dumps(request))

    result = CliRunner().invoke(
        main,
        [
            "snapshot",
            "evidence",
            "preview",
            str(request_path),
            "--no-json",
        ],
    )

    assert result.exit_code != 0
    assert "unexpected field" in result.output
    assert "PRIVATE_DATA_SENTINEL" not in result.output


@pytest.mark.integration
def test_evidence_export_reports_existing_destination(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    request_path = _write_request(tmp_path)
    preview_result = runner.invoke(
        main,
        [
            "snapshot",
            "evidence",
            "preview",
            str(request_path),
            "--no-json",
        ],
    )
    approval_digest = json.loads(preview_result.output)["approval_digest"]
    out = tmp_path / "existing"
    out.mkdir()
    marker = out / "marker"
    marker.write_text("preserve")

    result = runner.invoke(
        main,
        [
            "snapshot",
            "evidence",
            "export",
            str(request_path),
            "--out",
            str(out),
            "--approve",
            approval_digest,
            "--no-json",
        ],
    )

    assert result.exit_code == 2
    assert "destination already exists" in result.output
    assert marker.read_text() == "preserve"


@pytest.mark.integration
def test_reviewer_validates_exported_bundle(tmp_path: Path) -> None:
    runner = CliRunner()
    out, approval_digest = _export_bundle_with_cli(runner, tmp_path)

    validated = runner.invoke(
        main,
        [
            "snapshot",
            "evidence",
            "validate",
            str(out),
            "--expect",
            approval_digest,
            "--no-json",
        ],
    )

    assert validated.exit_code == 0, validated.output
    assert json.loads(validated.output) == {
        "approval_digest": approval_digest,
        "conclusion": "advance_a",
    }


@pytest.mark.integration
def test_reviewer_validation_rejects_extra_bundle_content(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    out, approval_digest = _export_bundle_with_cli(runner, tmp_path)
    (out / "raw-results.json").write_text("PRIVATE_DATA_SENTINEL")

    validated = runner.invoke(
        main,
        [
            "snapshot",
            "evidence",
            "validate",
            str(out),
            "--expect",
            approval_digest,
            "--no-json",
        ],
    )

    assert validated.exit_code != 0
    assert "exactly the expected regular files" in validated.output
    assert "PRIVATE_DATA_SENTINEL" not in validated.output


@pytest.mark.integration
def test_reviewer_validation_rejects_symlinked_artifact(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    out, approval_digest = _export_bundle_with_cli(runner, tmp_path)
    findings = out / "findings.md"
    outside = tmp_path / "outside.md"
    outside.write_text(findings.read_text())
    findings.unlink()
    findings.symlink_to(outside)

    validated = runner.invoke(
        main,
        [
            "snapshot",
            "evidence",
            "validate",
            str(out),
            "--expect",
            approval_digest,
            "--no-json",
        ],
    )

    assert validated.exit_code != 0
    assert "not a regular file" in validated.output


@pytest.mark.integration
def test_reviewer_validation_rejects_deep_unapproved_tree(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    out, approval_digest = _export_bundle_with_cli(runner, tmp_path)
    unexpected = out / "unexpected"
    current = unexpected
    for _ in range(150):
        current.mkdir()
        current = current / "d"

    validated = runner.invoke(
        main,
        [
            "snapshot",
            "evidence",
            "validate",
            str(out),
            "--expect",
            approval_digest,
            "--no-json",
        ],
    )

    assert validated.exit_code == 2
    assert "exactly the expected regular files" in validated.output
    assert "Traceback" not in validated.output
