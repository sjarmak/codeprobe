"""Content-bound approval digests for evidence artifacts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from codeprobe.snapshot.evidence_findings import render_findings

APPROVAL_PLACEHOLDER = "sha256:" + ("0" * 64)


def is_approval_digest(value: object) -> bool:
    """Return whether a value has the fixed approval-digest encoding."""
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def approval_digest_for_artifacts(
    run: Mapping[str, Any],
    sample: Mapping[str, Any],
    aggregate: Mapping[str, Any],
    support: Mapping[str, Any],
) -> str:
    """Bind normalized artifact content while excluding self-references."""
    attestation = sample["data_owner_attestation"]
    normalized_aggregate = {
        **aggregate,
        "approval_digest": APPROVAL_PLACEHOLDER,
    }
    normalized = {
        "run-manifest.json": {
            **run,
            "approval_digest": APPROVAL_PLACEHOLDER,
        },
        "sample-attestation.json": {
            **sample,
            "approval_digest": APPROVAL_PLACEHOLDER,
            "data_owner_attestation": {
                **attestation,
                "approval_digest": APPROVAL_PLACEHOLDER,
            },
        },
        "aggregate-results.json": normalized_aggregate,
        "findings.md": render_findings(normalized_aggregate),
        "support-log.json": {
            **support,
            "approval_digest": APPROVAL_PLACEHOLDER,
        },
    }
    payload = _canonical_json(normalized).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def approval_digest_for_documents(documents: Mapping[str, str]) -> str:
    """Bind trusted, generated versions of the five artifacts."""
    return approval_digest_for_artifacts(
        json.loads(documents["run-manifest.json"]),
        json.loads(documents["sample-attestation.json"]),
        json.loads(documents["aggregate-results.json"]),
        json.loads(documents["support-log.json"]),
    )
