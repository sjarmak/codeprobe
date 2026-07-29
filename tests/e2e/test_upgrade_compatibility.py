"""Contracts for the prior-release-to-candidate upgrade harness."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from scripts.e2e.upgrade_compatibility import (
    PRIOR_RELEASE_SHA256,
    PRIOR_RELEASE_VERSION,
    UpgradeCompatibilityError,
    runtime_environment,
    successful_envelope,
    validate_interpretation,
    validate_wheel,
)


def test_prior_release_contract_is_exactly_pinned() -> None:
    assert PRIOR_RELEASE_VERSION == "0.11.0"
    assert (
        PRIOR_RELEASE_SHA256
        == "a7797a1f4be4a6b4bd9ce73cb4ac868d8e26e2d4a23a3ecda040ee19105bfbf5"
    )


def test_validate_wheel_accepts_exact_pinned_prior_artifact(tmp_path: Path) -> None:
    wheel = tmp_path / "codeprobe-0.11.0-py3-none-any.whl"
    wheel.write_bytes(b"prior wheel")
    digest = hashlib.sha256(b"prior wheel").hexdigest()

    validate_wheel(wheel, expected_sha256=digest)


def test_validate_wheel_rejects_missing_or_substituted_artifact(tmp_path: Path) -> None:
    missing = tmp_path / "codeprobe-0.11.0-py3-none-any.whl"
    with pytest.raises(UpgradeCompatibilityError, match="cannot be read"):
        validate_wheel(missing, expected_sha256=PRIOR_RELEASE_SHA256)

    missing.write_bytes(b"substituted")
    with pytest.raises(UpgradeCompatibilityError, match="digest"):
        validate_wheel(missing, expected_sha256=PRIOR_RELEASE_SHA256)


def test_successful_envelope_accepts_prior_cli_human_output_before_json() -> None:
    result = subprocess.CompletedProcess(
        args=["codeprobe", "mine"],
        returncode=0,
        stdout='Mining summary\n{"ok": true, "data": {"task_count": 1}}\n',
        stderr="",
    )

    assert successful_envelope(result, "prior mine")["data"]["task_count"] == 1


def test_upgrade_installs_candidate_dependencies() -> None:
    script = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "e2e"
        / "upgrade_compatibility.py"
    ).read_text(encoding="utf-8")

    assert '"--no-deps"' not in script


def test_runtime_environment_does_not_expose_parent_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-cross-boundary")
    monkeypatch.setenv("PIP_INDEX_URL", "https://user:password@example.invalid")
    monkeypatch.setenv("PATH", os.defpath)

    env = runtime_environment(tmp_path)

    assert env == {
        "CODEPROBE_TENANT": "release-upgrade-compatibility",
        "HOME": str(tmp_path),
        "PATH": os.defpath,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": None},
        {"data": {"report": {}}},
        {"data": {"report": {"summaries": []}}},
        {"data": {"report": {"summaries": [{"cost_coverage": 0.0}]}}},
    ],
)
def test_interpretation_requires_one_fully_measured_prior_result(
    payload: dict[str, object],
) -> None:
    with pytest.raises(UpgradeCompatibilityError, match="incomplete"):
        validate_interpretation(payload)
