"""Validation contract for release-blocking enterprise journey evidence."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

SCHEMA_VERSION: Final[str] = "codeprobe.enterprise-journey.v1"
MAX_EVIDENCE_BYTES: Final[int] = 1024 * 1024
DRY_PRODUCER_AGENTS: Final[frozenset[str]] = frozenset({"e2e-stub"})
REAL_PRODUCER_AGENTS: Final[frozenset[str]] = frozenset({"claude", "copilot"})
REQUIRED_STEPS: Final[tuple[str, ...]] = (
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
REQUIRED_BOOLEAN_INVARIANTS: Final[tuple[str, ...]] = (
    "candidate_version_matches",
    "container_isolation",
    "evidence_digest_bound",
    "output_locations_valid",
    "structured_errors",
    "worktree_isolation",
)
REQUIRED_NETWORK_VARIANTS: Final[tuple[str, ...]] = (
    "proxy-private-ca",
    "offline-private-registry",
)
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST_REFERENCE = re.compile(
    r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}\Z"
)


class EnterpriseJourneyEvidenceError(ValueError):
    """Raised when journey evidence cannot authorize a release."""


def validate_enterprise_journey_evidence(
    path: Path,
    *,
    expected_version: str,
    expected_commit: str,
    expected_wheel_sha256: str,
    expected_agent_image: str,
    expected_scoring_image: str,
    max_cost_usd: float,
) -> dict[str, Any]:
    """Load and validate one candidate-bound real-agent journey record."""
    evidence = _load_evidence(path)
    _validate_root(evidence)
    _validate_candidate(
        evidence["candidate"],
        expected_version=expected_version,
        expected_commit=expected_commit,
        expected_wheel_sha256=expected_wheel_sha256,
        expected_agent_image=expected_agent_image,
        expected_scoring_image=expected_scoring_image,
    )
    _validate_producer(evidence["producer"])
    _validate_budget(evidence["budget"], max_cost_usd=max_cost_usd)
    _validate_steps(evidence["steps"])
    _validate_invariants(evidence["invariants"])
    _validate_network_variants(evidence["network_variants"])
    _validate_secret_scan(evidence["secret_scan"])
    return evidence


def _load_evidence(path: Path) -> dict[str, Any]:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise EnterpriseJourneyEvidenceError(
            "enterprise journey evidence cannot be read"
        ) from exc
    if len(content) > MAX_EVIDENCE_BYTES:
        raise EnterpriseJourneyEvidenceError("enterprise journey evidence is too large")
    try:
        raw = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnterpriseJourneyEvidenceError(
            "enterprise journey evidence is not valid JSON"
        ) from exc
    if not isinstance(raw, dict):
        raise EnterpriseJourneyEvidenceError(
            "enterprise journey evidence root must be an object"
        )
    return raw


def _validate_root(evidence: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "candidate",
        "producer",
        "budget",
        "steps",
        "invariants",
        "network_variants",
        "secret_scan",
    }
    if set(evidence) != required:
        raise EnterpriseJourneyEvidenceError(
            "enterprise journey evidence fields do not match the release contract"
        )
    if evidence["schema_version"] != SCHEMA_VERSION:
        raise EnterpriseJourneyEvidenceError(
            "enterprise journey evidence schema_version is unsupported"
        )


def _validate_candidate(
    raw: Any,
    *,
    expected_version: str,
    expected_commit: str,
    expected_wheel_sha256: str,
    expected_agent_image: str,
    expected_scoring_image: str,
) -> None:
    candidate = _object(raw, "candidate")
    _exact_fields(
        candidate,
        {
            "version",
            "commit",
            "wheel_sha256",
            "agent_image",
            "scoring_image",
        },
        "candidate",
    )
    if candidate["version"] != expected_version:
        raise EnterpriseJourneyEvidenceError("candidate version does not match")
    if not _COMMIT_SHA.fullmatch(expected_commit) or candidate["commit"] != expected_commit:
        raise EnterpriseJourneyEvidenceError("candidate commit does not match")
    if (
        not _HEX_SHA256.fullmatch(expected_wheel_sha256)
        or candidate["wheel_sha256"] != expected_wheel_sha256
    ):
        raise EnterpriseJourneyEvidenceError("candidate wheel SHA-256 does not match")
    _validate_image(
        candidate["agent_image"],
        expected_agent_image,
        "candidate agent image does not match",
    )
    _validate_image(
        candidate["scoring_image"],
        expected_scoring_image,
        "candidate scoring image does not match",
    )


def _validate_image(raw: Any, expected: str, error: str) -> None:
    if not _DIGEST_REFERENCE.fullmatch(expected) or raw != expected:
        raise EnterpriseJourneyEvidenceError(error)


def _validate_producer(raw: Any) -> None:
    producer = _object(raw, "producer")
    _exact_fields(producer, {"agent", "kind"}, "producer")
    agent = producer["agent"]
    is_real = (
        isinstance(agent, str)
        and bool(agent.strip())
        and agent == agent.strip()
        and agent not in DRY_PRODUCER_AGENTS
        and agent in REAL_PRODUCER_AGENTS
        and producer["kind"] == "real"
    )
    if not is_real:
        raise EnterpriseJourneyEvidenceError(
            "enterprise journey requires a real producer agent"
        )


def _validate_budget(raw: Any, *, max_cost_usd: float) -> None:
    budget = _object(raw, "budget")
    _exact_fields(budget, {"max_cost_usd", "observed_cost_usd"}, "budget")
    declared = _finite_number(budget["max_cost_usd"], "budget.max_cost_usd")
    observed = _finite_number(budget["observed_cost_usd"], "budget.observed_cost_usd")
    if max_cost_usd <= 0 or declared != max_cost_usd:
        raise EnterpriseJourneyEvidenceError("release budget does not match")
    if observed < 0 or observed > declared:
        raise EnterpriseJourneyEvidenceError("observed cost exceeds release budget")


def _validate_steps(raw: Any) -> None:
    steps = _array(raw, "steps")
    by_name: dict[str, Mapping[str, Any]] = {}
    for raw_step in steps:
        step = _object(raw_step, "steps entry")
        _exact_fields(step, {"name", "status"}, "steps entry")
        name = step["name"]
        if not isinstance(name, str) or name in by_name:
            raise EnterpriseJourneyEvidenceError(
                "enterprise journey required steps are malformed"
            )
        by_name[name] = step
    if tuple(by_name) != REQUIRED_STEPS:
        raise EnterpriseJourneyEvidenceError(
            "enterprise journey required steps are missing or out of order"
        )
    for name in REQUIRED_STEPS:
        if by_name[name]["status"] != "passed":
            raise EnterpriseJourneyEvidenceError(f"{name} must have status passed")


def _validate_invariants(raw: Any) -> None:
    invariants = _object(raw, "invariants")
    required = {*REQUIRED_BOOLEAN_INVARIANTS, "source_checkout_reads"}
    _exact_fields(invariants, required, "invariants")
    for name in REQUIRED_BOOLEAN_INVARIANTS:
        if invariants[name] is not True:
            raise EnterpriseJourneyEvidenceError(f"invariant {name} must pass")
    reads = invariants["source_checkout_reads"]
    if not isinstance(reads, int) or isinstance(reads, bool) or reads != 0:
        raise EnterpriseJourneyEvidenceError(
            "invariant source_checkout_reads must be zero"
        )


def _validate_network_variants(raw: Any) -> None:
    variants = _array(raw, "network_variants")
    if len(variants) != len(REQUIRED_NETWORK_VARIANTS):
        raise EnterpriseJourneyEvidenceError("network variants are incomplete")
    for expected_name, raw_variant in zip(
        REQUIRED_NETWORK_VARIANTS, variants, strict=True
    ):
        variant = _object(raw_variant, "network variant")
        _exact_fields(
            variant,
            {"name", "status", "public_network_attempts"},
            "network variant",
        )
        attempts = variant["public_network_attempts"]
        if (
            variant["name"] != expected_name
            or variant["status"] != "passed"
            or not isinstance(attempts, int)
            or isinstance(attempts, bool)
            or attempts != 0
        ):
            raise EnterpriseJourneyEvidenceError(
                f"network variant {expected_name} did not pass without public access"
            )


def _validate_secret_scan(raw: Any) -> None:
    scan = _object(raw, "secret_scan")
    _exact_fields(scan, {"values_checked", "leaks"}, "secret_scan")
    values_checked = scan["values_checked"]
    leaks = scan["leaks"]
    if (
        not isinstance(values_checked, int)
        or isinstance(values_checked, bool)
        or values_checked < 1
        or not isinstance(leaks, int)
        or isinstance(leaks, bool)
        or leaks != 0
    ):
        raise EnterpriseJourneyEvidenceError("secret scan did not pass")


def _object(raw: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(raw, dict):
        raise EnterpriseJourneyEvidenceError(f"{field} must be an object")
    return raw


def _array(raw: Any, field: str) -> Sequence[Any]:
    if not isinstance(raw, list):
        raise EnterpriseJourneyEvidenceError(f"{field} must be an array")
    return raw


def _exact_fields(
    value: Mapping[str, Any],
    expected: set[str],
    field: str,
) -> None:
    if set(value) != expected:
        raise EnterpriseJourneyEvidenceError(f"{field} fields are invalid")


def _finite_number(raw: Any, field: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise EnterpriseJourneyEvidenceError(f"{field} must be a finite number")
    value = float(raw)
    if not math.isfinite(value):
        raise EnterpriseJourneyEvidenceError(f"{field} must be a finite number")
    return value


__all__ = [
    "DRY_PRODUCER_AGENTS",
    "EnterpriseJourneyEvidenceError",
    "REAL_PRODUCER_AGENTS",
    "REQUIRED_NETWORK_VARIANTS",
    "REQUIRED_STEPS",
    "SCHEMA_VERSION",
    "validate_enterprise_journey_evidence",
]
