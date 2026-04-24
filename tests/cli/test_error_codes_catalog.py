"""Tests for the public error-code catalog (src/codeprobe/cli/error_codes.json).

These tests are the catalog's public-contract spec:

- The file must be valid JSON.
- The top-level shape is ``{"version": "1", "codes": [...]}``.
- Every entry carries a stable set of keys
  (code, kind, exit_code, since, description, remediation_pattern).
- Codes are UPPER_SNAKE_CASE and unique.
- ``kind`` is exactly one of ``"prescriptive"`` or ``"diagnostic"``.
- The catalog contains every error code required by the Agent-Friendly CLI
  PRD §6.4 with the correct ``kind``.
- ``BUDGET_EXCEEDED`` is intentionally ``diagnostic`` — no prescriptive
  next-try pattern is allowed for it (load-bearing safety decision).
"""

from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path

import pytest

CATALOG_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "codeprobe"
    / "cli"
    / "error_codes.json"
)

CODE_REGEX = re.compile(r"^[A-Z][A-Z0-9_]+$")

REQUIRED_KEYS = {
    "code",
    "kind",
    "exit_code",
    "since",
    "description",
    "remediation_pattern",
}

ALLOWED_KINDS = {"prescriptive", "diagnostic"}

# (code, expected_kind) pairs from PRD §6.4.
EXPECTED_CODES: list[tuple[str, str]] = [
    ("NARRATIVE_SOURCE_UNDETECTABLE", "prescriptive"),
    ("MUTEX_FLAGS", "prescriptive"),
    ("TRACE_OVERFLOW_FIRED", "prescriptive"),
    ("NO_EXPERIMENT", "diagnostic"),
    ("AMBIGUOUS_EXPERIMENT", "prescriptive"),
    ("NO_TASKS", "diagnostic"),
    ("NO_SUITE_MATCH", "diagnostic"),
    ("INVALID_PERMISSION_MODE", "prescriptive"),
    ("INTERRUPTED", "diagnostic"),
    ("TRACE_BUDGET_EXCEEDED", "prescriptive"),
    ("SOURCE_EXPORT_REQUIRES_ACK", "prescriptive"),
    ("CANARY_PROOF_FAILED", "diagnostic"),
    ("CANARY_PROOF_REQUIRED", "prescriptive"),
    ("CANARY_MISMATCH", "diagnostic"),
    ("CANARY_GATE_FAILED", "diagnostic"),
    ("SNAPSHOT_CREATE_FAILED", "diagnostic"),
    ("SNAPSHOT_VERIFY_FAILED", "diagnostic"),
    ("METADATA_MISSING", "diagnostic"),
    ("METADATA_INVALID", "diagnostic"),
    ("CAPABILITY_DRIFT", "diagnostic"),
    ("UNKNOWN_BACKEND", "prescriptive"),
    ("NO_BACKENDS_CONFIGURED", "diagnostic"),
    ("OFFLINE_PREFLIGHT_FAILED", "diagnostic"),
    ("INVALID_GIT_URL", "prescriptive"),
    ("CLONE_FAILED", "diagnostic"),
    ("CALIBRATION_REJECTED", "diagnostic"),
    ("DOCTOR_CHECKS_FAILED", "diagnostic"),
    ("BUDGET_EXCEEDED", "diagnostic"),
    ("GOAL_UNDETECTABLE", "diagnostic"),
    ("SG_DISCOVERY_QUOTA_EXCEEDED", "diagnostic"),
    ("OFFLINE_NET_ATTEMPT", "diagnostic"),
    ("TENANT_REQUIRED_IN_CI", "diagnostic"),
    ("STALE_USER_HOME_SKILL", "diagnostic"),
    ("LLM_UNAVAILABLE", "diagnostic"),
]


@pytest.fixture(scope="module")
def catalog() -> dict:
    assert CATALOG_PATH.exists(), f"missing catalog file: {CATALOG_PATH}"
    with CATALOG_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def test_catalog_is_valid_json(catalog: dict) -> None:
    # Loading succeeded via fixture; re-assert top-level type.
    assert isinstance(catalog, dict)


def test_catalog_version_is_one(catalog: dict) -> None:
    assert catalog.get("version") == "1"


def test_catalog_has_codes_array(catalog: dict) -> None:
    assert isinstance(catalog.get("codes"), list)
    assert len(catalog["codes"]) > 0


def test_every_entry_has_required_keys(catalog: dict) -> None:
    for entry in catalog["codes"]:
        missing = REQUIRED_KEYS - set(entry.keys())
        assert not missing, f"entry {entry.get('code')!r} missing keys: {missing}"


def test_every_code_matches_regex(catalog: dict) -> None:
    for entry in catalog["codes"]:
        code = entry["code"]
        assert CODE_REGEX.match(code), f"invalid code format: {code!r}"


def test_no_duplicate_codes(catalog: dict) -> None:
    codes = [entry["code"] for entry in catalog["codes"]]
    dupes = {c for c in codes if codes.count(c) > 1}
    assert not dupes, f"duplicate codes: {sorted(dupes)}"


def test_every_kind_is_allowed(catalog: dict) -> None:
    for entry in catalog["codes"]:
        assert (
            entry["kind"] in ALLOWED_KINDS
        ), f"entry {entry['code']!r} has invalid kind {entry['kind']!r}"


def test_exit_codes_are_ints(catalog: dict) -> None:
    for entry in catalog["codes"]:
        assert isinstance(
            entry["exit_code"], int
        ), f"entry {entry['code']!r} exit_code must be int"


def test_description_and_remediation_nonempty(catalog: dict) -> None:
    for entry in catalog["codes"]:
        assert entry["description"].strip(), f"{entry['code']}: empty description"
        assert entry[
            "remediation_pattern"
        ].strip(), f"{entry['code']}: empty remediation_pattern"


@pytest.mark.parametrize("code,expected_kind", EXPECTED_CODES)
def test_expected_code_present_with_correct_kind(
    catalog: dict, code: str, expected_kind: str
) -> None:
    entries = {e["code"]: e for e in catalog["codes"]}
    assert code in entries, f"catalog missing required code: {code}"
    assert entries[code]["kind"] == expected_kind, (
        f"code {code} expected kind={expected_kind!r}, "
        f"got {entries[code]['kind']!r}"
    )


def test_budget_exceeded_is_diagnostic(catalog: dict) -> None:
    """BUDGET_EXCEEDED must stay diagnostic — no prescriptive next-try (PRD §6.4)."""
    entries = {e["code"]: e for e in catalog["codes"]}
    assert entries["BUDGET_EXCEEDED"]["kind"] == "diagnostic"


def test_catalog_is_packaged(catalog: dict) -> None:
    """error_codes.json must be readable via importlib.resources so it ships
    in wheels/sdists (pyproject package-data wiring)."""
    with resources.files("codeprobe.cli").joinpath("error_codes.json").open(
        encoding="utf-8"
    ) as fh:
        data = json.load(fh)
    assert data["version"] == "1"
    assert data["codes"] == catalog["codes"]
