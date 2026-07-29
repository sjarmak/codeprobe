"""Release-gate contracts for clean-wheel enterprise journey evidence."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from acceptance.enterprise_journey import (
    EnterpriseJourneyEvidenceError,
    validate_enterprise_journey_evidence,
)
from scripts.enterprise_release_gate import main as release_gate_main

VERSION = "0.13.0"
COMMIT = "a" * 40
WHEEL_DIGEST = hashlib.sha256(b"candidate wheel").hexdigest()
MAX_COST_USD = 1.25
AGENT_IMAGE = "registry.example.test/platform/codeprobe-agent@sha256:" + "c" * 64
SCORING_IMAGE = "registry.example.test/platform/codeprobe-scoring@sha256:" + "d" * 64


def _evidence() -> dict[str, Any]:
    return {
        "schema_version": "codeprobe.enterprise-journey.v1",
        "candidate": {
            "version": VERSION,
            "commit": COMMIT,
            "wheel_sha256": WHEEL_DIGEST,
            "agent_image": AGENT_IMAGE,
            "scoring_image": SCORING_IMAGE,
        },
        "producer": {"agent": "claude", "kind": "real"},
        "budget": {"max_cost_usd": MAX_COST_USD, "observed_cost_usd": 0.42},
        "steps": [
            {"name": name, "status": "passed"}
            for name in (
                "install-wheel",
                "bootstrap",
                "doctor",
                "assess",
                "mine",
                "run",
                "interpret",
                "evidence-preview",
                "evidence-export",
                "evidence-validate",
            )
        ],
        "invariants": {
            "candidate_version_matches": True,
            "container_isolation": True,
            "evidence_digest_bound": True,
            "output_locations_valid": True,
            "source_checkout_reads": 0,
            "structured_errors": True,
            "worktree_isolation": True,
        },
        "network_variants": [
            {
                "name": "proxy-private-ca",
                "status": "passed",
                "public_network_attempts": 0,
            },
            {
                "name": "offline-private-registry",
                "status": "passed",
                "public_network_attempts": 0,
            },
        ],
        "secret_scan": {"values_checked": 3, "leaks": 0},
    }


def _write(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "enterprise-journey.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _validate(tmp_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    return validate_enterprise_journey_evidence(
        _write(tmp_path, payload),
        expected_version=VERSION,
        expected_commit=COMMIT,
        expected_wheel_sha256=WHEEL_DIGEST,
        expected_agent_image=AGENT_IMAGE,
        expected_scoring_image=SCORING_IMAGE,
        max_cost_usd=MAX_COST_USD,
    )


def test_accepts_complete_candidate_bound_real_agent_evidence(tmp_path: Path) -> None:
    validated = _validate(tmp_path, _evidence())

    assert validated["producer"] == {"agent": "claude", "kind": "real"}
    assert validated["candidate"]["wheel_sha256"] == WHEEL_DIGEST


@pytest.mark.parametrize("agent", ["e2e-stub", "", "   "])
def test_rejects_stub_or_missing_producer(tmp_path: Path, agent: str) -> None:
    evidence = _evidence()
    evidence["producer"]["agent"] = agent

    with pytest.raises(EnterpriseJourneyEvidenceError, match="real producer"):
        _validate(tmp_path, evidence)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("version", "0.13.1", "candidate version"),
        ("commit", "b" * 40, "candidate commit"),
        ("wheel_sha256", hashlib.sha256(b"other").hexdigest(), "wheel SHA-256"),
        (
            "agent_image",
            "registry.example.test/other@sha256:" + "e" * 64,
            "agent image",
        ),
        (
            "scoring_image",
            "registry.example.test/other@sha256:" + "f" * 64,
            "scoring image",
        ),
    ],
)
def test_rejects_evidence_for_another_candidate(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    evidence = _evidence()
    evidence["candidate"][field] = value

    with pytest.raises(EnterpriseJourneyEvidenceError, match=message):
        _validate(tmp_path, evidence)


def test_rejects_missing_or_failed_journey_step(tmp_path: Path) -> None:
    missing = _evidence()
    missing["steps"] = missing["steps"][:-1]
    with pytest.raises(EnterpriseJourneyEvidenceError, match="required steps"):
        _validate(tmp_path, missing)

    failed = _evidence()
    failed["steps"][5]["status"] = "failed"
    with pytest.raises(EnterpriseJourneyEvidenceError, match="run.*passed"):
        _validate(tmp_path, failed)


def test_rejects_failed_isolation_or_secret_invariant(tmp_path: Path) -> None:
    isolation = _evidence()
    isolation["invariants"]["source_checkout_reads"] = 1
    with pytest.raises(EnterpriseJourneyEvidenceError, match="source_checkout_reads"):
        _validate(tmp_path, isolation)

    secret = _evidence()
    secret["secret_scan"]["leaks"] = 1
    with pytest.raises(EnterpriseJourneyEvidenceError, match="secret"):
        _validate(tmp_path, secret)


def test_rejects_incomplete_or_network_touching_offline_variant(tmp_path: Path) -> None:
    evidence = _evidence()
    offline = evidence["network_variants"][1]
    offline["public_network_attempts"] = 1

    with pytest.raises(EnterpriseJourneyEvidenceError, match="offline-private-registry"):
        _validate(tmp_path, evidence)


def test_rejects_observed_cost_above_declared_release_budget(tmp_path: Path) -> None:
    evidence = _evidence()
    evidence["budget"]["observed_cost_usd"] = MAX_COST_USD + 0.01

    with pytest.raises(EnterpriseJourneyEvidenceError, match="budget"):
        _validate(tmp_path, evidence)


def test_validation_error_never_echoes_untrusted_secret_value(tmp_path: Path) -> None:
    sentinel = "release-secret-SENTINEL"
    evidence = copy.deepcopy(_evidence())
    evidence["producer"]["agent"] = sentinel
    evidence["producer"]["kind"] = "stub"

    with pytest.raises(EnterpriseJourneyEvidenceError) as exc_info:
        _validate(tmp_path, evidence)

    assert sentinel not in str(exc_info.value)


def test_release_gate_cli_binds_the_exact_wheel_and_images(tmp_path: Path) -> None:
    wheel = tmp_path / "codeprobe-0.13.0-py3-none-any.whl"
    wheel.write_bytes(b"candidate wheel")
    evidence_path = _write(tmp_path, _evidence())

    assert (
        release_gate_main(
            [
                "--evidence",
                str(evidence_path),
                "--wheel",
                str(wheel),
                "--expected-version",
                VERSION,
                "--expected-commit",
                COMMIT,
                "--expected-agent-image",
                AGENT_IMAGE,
                "--expected-scoring-image",
                SCORING_IMAGE,
                "--max-cost-usd",
                str(MAX_COST_USD),
            ]
        )
        == 0
    )

    wheel.write_bytes(b"substituted wheel")
    assert (
        release_gate_main(
            [
                "--evidence",
                str(evidence_path),
                "--wheel",
                str(wheel),
                "--expected-version",
                VERSION,
                "--expected-commit",
                COMMIT,
                "--expected-agent-image",
                AGENT_IMAGE,
                "--expected-scoring-image",
                SCORING_IMAGE,
                "--max-cost-usd",
                str(MAX_COST_USD),
            ]
        )
        == 1
    )
