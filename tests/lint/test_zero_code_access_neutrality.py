"""Keep generic zero-code-access controls free of engagement identity."""

from __future__ import annotations

from pathlib import Path

from codeprobe.snapshot.evidence_models import ACTOR_ROLES

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_PATHS = (
    REPO_ROOT / "docs" / "EVIDENCE_BUNDLE.md",
    REPO_ROOT / "src" / "codeprobe" / "cli" / "evidence_cmd.py",
    *sorted(
        (REPO_ROOT / "src" / "codeprobe" / "snapshot").glob("evidence_*.py")
    ),
)
FORBIDDEN_ENGAGEMENT_MARKERS = (
    "codeprobe-2z76",
    "cp-zca-pilot",
    "field_engineering",
    "other_sourcegraph_personnel",
    "participant",
    "participant_",
    "solutions_engineering",
    "sourcegraph",
)


def test_zero_code_access_product_surface_is_engagement_agnostic() -> None:
    assert not (REPO_ROOT / "docs" / "pilot" / "zero-code-access").exists()
    assert not (
        REPO_ROOT / "docs" / "strategy" / "zero_code_access_validation.md"
    ).exists()

    for path in EVIDENCE_PATHS:
        content = path.read_text(encoding="utf-8").casefold()
        assert all(
            marker not in content for marker in FORBIDDEN_ENGAGEMENT_MARKERS
        ), f"{path.relative_to(REPO_ROOT)} contains engagement-specific language"


def test_support_actor_roles_are_provider_neutral() -> None:
    assert ACTOR_ROLES == (
        "data_owner_security",
        "data_owner_technical_owner",
        "other_provider_personnel",
        "provider_engineering",
        "provider_support",
    )
