"""Focused tests for the clean-wheel enterprise journey mechanics."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from codeprobe.snapshot.evidence_bundle import (
    load_evidence_request,
    preview_evidence_bundle,
)
from scripts.e2e.enterprise_artifacts import (
    EnterpriseHarnessError,
    assert_no_secret_values,
    build_evidence_request,
    parse_envelope,
    validate_image_labels,
)
from scripts.e2e.enterprise_install import installed_version
from scripts.e2e.enterprise_journey import _validate_args
from scripts.e2e.enterprise_runtime import base_environment


def _report() -> dict[str, Any]:
    return {
        "summaries": [
            {
                "label": label,
                "total_tasks": 1,
                "errored_count": 0,
                "mean_score": score,
                "ci_lower": score,
                "ci_upper": score,
                "total_cost_usd": cost,
                "cost_coverage": 1.0,
                "mean_duration_sec": duration,
                "total_duration_sec": duration,
                "distinct_task_count": 1,
            }
            for label, score, cost, duration in (
                ("A", 1.0, 0.12, 2.0),
                ("B", 0.0, 0.10, 1.5),
            )
        ],
        "comparisons": [
            {
                "config_a": "A",
                "config_b": "B",
                "score_diff": 1.0,
                "cost_diff": 0.02,
                "speed_diff": 0.5,
                "p_value": None,
                "effect_size": None,
                "effect_size_method": "",
                "ci_lower": 1.0,
                "ci_upper": 1.0,
                "comparable": False,
                "refusal_reason": "Need at least 3 paired tasks",
            }
        ],
        "tasks": [
            {"config": config, "task_id": "task-0001"}
            for config in ("A", "B")
        ],
    }


def _experiment() -> dict[str, Any]:
    return {
        "configs": [
            {"label": label, "agent": "claude", "model": None}
            for label in ("A", "B")
        ]
    }


def test_builds_valid_insufficient_evidence_request_from_real_report(
    tmp_path: Path,
) -> None:
    request = build_evidence_request(
        report=_report(),
        experiment=_experiment(),
        candidate_version="0.13.0",
    )
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")

    preview = preview_evidence_bundle(load_evidence_request(request_path))
    aggregate = json.loads(
        next(
            artifact.content
            for artifact in preview.artifacts
            if artifact.filename == "aggregate-results.json"
        )
    )

    assert aggregate["conclusion"] == "insufficient_evidence"
    assert aggregate["paired_task_count"] == 1
    assert aggregate["repeats_per_task"] == 1
    assert aggregate["validity_warnings"] == [
        "below_paired_task_floor",
        "incomplete_repeats",
        "report_refused",
        "sample_not_representative",
    ]


def test_request_builder_rejects_missing_cost_instead_of_claiming_zero() -> None:
    report = _report()
    report["summaries"][0]["total_cost_usd"] = None

    with pytest.raises(EnterpriseHarnessError, match="cost telemetry"):
        build_evidence_request(
            report=report,
            experiment=_experiment(),
            candidate_version="0.13.0",
        )


def test_request_builder_rejects_incomplete_real_agent_cost_coverage() -> None:
    report = _report()
    report["summaries"][0]["cost_coverage"] = 0.0

    with pytest.raises(EnterpriseHarnessError, match="cost telemetry"):
        build_evidence_request(
            report=report,
            experiment=_experiment(),
            candidate_version="0.13.0",
        )


def test_parse_envelope_requires_one_successful_structured_record() -> None:
    envelope = {
        "record_type": "envelope",
        "ok": True,
        "command": "doctor",
        "data": {"checks": []},
    }
    assert parse_envelope(json.dumps(envelope) + "\n", "doctor") == envelope

    with pytest.raises(EnterpriseHarnessError, match="successful JSON envelope"):
        parse_envelope('{"record_type":"envelope","ok":false}\n', "doctor")
    with pytest.raises(EnterpriseHarnessError, match="successful JSON envelope"):
        parse_envelope("not-json\n", "doctor")


def test_secret_scan_checks_command_output_and_artifacts_without_echoing_value(
    tmp_path: Path,
) -> None:
    secret = "enterprise-secret-SENTINEL"
    clean = tmp_path / "clean.json"
    clean.write_text('{"ok": true}', encoding="utf-8")
    assert assert_no_secret_values([clean], ["ordinary output"], [secret]) == 1

    leaked = tmp_path / "leaked.txt"
    leaked.write_text(f"token={secret}", encoding="utf-8")
    with pytest.raises(EnterpriseHarnessError) as exc_info:
        assert_no_secret_values([leaked], ["ordinary output"], [secret])
    assert secret not in str(exc_info.value)


def test_secret_scan_rejects_short_or_empty_secret_contract(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    artifact.write_text("content", encoding="utf-8")

    with pytest.raises(EnterpriseHarnessError, match="secret values"):
        assert_no_secret_values([artifact], [], ["short"])


def test_published_image_labels_bind_version_and_candidate_commit() -> None:
    labels = {
        "org.opencontainers.image.version": "0.13.0",
        "org.opencontainers.image.revision": "a" * 40,
    }
    validate_image_labels(labels, version="0.13.0", commit="a" * 40)

    labels["org.opencontainers.image.revision"] = "b" * 40
    with pytest.raises(EnterpriseHarnessError, match="candidate labels"):
        validate_image_labels(labels, version="0.13.0", commit="a" * 40)


def test_runtime_exposes_release_credential_only_under_selected_agent_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEPROBE_RELEASE_AGENT_CREDENTIAL", "release-secret-value")

    environment = base_environment(
        home=tmp_path / "home",
        shim_bin=tmp_path / "bin",
        config_path=tmp_path / "images.json",
        credential_env="ANTHROPIC_API_KEY",
        credential_value="release-secret-value",
        agent_image="agent@sha256:" + "a" * 64,
        scoring_image="scoring@sha256:" + "b" * 64,
    )

    assert environment["ANTHROPIC_API_KEY"] == "release-secret-value"
    assert "CODEPROBE_RELEASE_AGENT_CREDENTIAL" not in environment


def test_wheel_setup_subprocess_does_not_inherit_release_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEPROBE_RELEASE_AGENT_CREDENTIAL", "release-secret-value")
    captured_environment: dict[str, str] = {}

    def fake_run(
        argv: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        captured_environment.update(kwargs["env"])  # type: ignore[arg-type]
        payload = json.dumps(
            {"version": "0.13.0", "path": str(tmp_path / "codeprobe" / "__init__.py")}
        )
        return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert installed_version(tmp_path / "python")[0] == "0.13.0"
    assert "CODEPROBE_RELEASE_AGENT_CREDENTIAL" not in captured_environment


def _journey_args(max_cost_usd: float = 1.25) -> argparse.Namespace:
    return argparse.Namespace(
        agent="claude",
        credential_env="ANTHROPIC_API_KEY",
        max_cost_usd=max_cost_usd,
        candidate_commit="a" * 40,
        agent_image="agent@sha256:" + "a" * 64,
        scoring_image="scoring@sha256:" + "b" * 64,
    )


@pytest.mark.parametrize("max_cost_usd", [math.nan, math.inf, -math.inf])
def test_journey_rejects_non_finite_budget_before_provider_execution(
    max_cost_usd: float,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEPROBE_RELEASE_AGENT_CREDENTIAL", "release-secret-value")

    with pytest.raises(EnterpriseHarnessError, match="budget"):
        _validate_args(_journey_args(max_cost_usd))


def test_journey_consumes_generic_release_credential_before_subprocesses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEPROBE_RELEASE_AGENT_CREDENTIAL", "release-secret-value")

    assert _validate_args(_journey_args()) == "release-secret-value"
    assert "CODEPROBE_RELEASE_AGENT_CREDENTIAL" not in os.environ
