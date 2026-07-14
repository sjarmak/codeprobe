"""Tests for scripts/check_bead_reachability.py.

Exercises ``check_bead`` directly against constructed bd JSON payloads,
mocking ``fetch_bead``/``is_ancestor`` so no real ``bd`` store or git
history is required. Each test pins one of the four rules documented in
the script's module docstring, plus the two bugs the 2026-07-13 branch
cleanup pass found in the wild:

- ``codeprobe-3cs``/``codeprobe-1gg`` closed with a ``close_reason`` citing
  an unreachable SHA (``94d357c``) that only ``metadata.evidence
  .artifact_path`` was ever checked against — the prose claim itself was
  never verified.
- Doc-only ``evidence.artifact_path`` values silently passed with zero
  verification of any underlying code claim.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "check_bead_reachability.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "check_bead_reachability", _MODULE_PATH
)
assert _SPEC is not None and _SPEC.loader is not None
cbr = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = cbr
_SPEC.loader.exec_module(cbr)


def _bead(
    *,
    status: str = "closed",
    metadata: dict[str, str] | None = None,
    close_reason: str = "",
    notes: str = "",
) -> dict:
    return {
        "id": "codeprobe-test",
        "status": status,
        "metadata": metadata or {},
        "close_reason": close_reason,
        "notes": notes,
    }


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    return tmp_path


def _check(
    bead: dict, *, reachable: set[str] = frozenset(), repo_root: Path
) -> cbr.BeadCheck:
    with (
        patch.object(cbr, "fetch_bead", return_value=bead),
        patch.object(
            cbr,
            "is_ancestor",
            side_effect=lambda sha, branch, root: sha in reachable,
        ),
    ):
        return cbr.check_bead("codeprobe-test", "main", repo_root)


class TestUnreachableSha:
    def test_reachable_sha_passes(self, repo_root: Path) -> None:
        bead = _bead(metadata={"evidence.artifact_path": "git:abc1234"})
        result = _check(bead, reachable={"abc1234"}, repo_root=repo_root)
        assert not result.violations
        assert result.shas_checked == ["abc1234"]

    def test_unreachable_sha_flagged(self, repo_root: Path) -> None:
        bead = _bead(metadata={"evidence.artifact_path": "git:deadbee"})
        result = _check(bead, reachable=set(), repo_root=repo_root)
        rules = [v.rule for v in result.violations]
        assert "unreachable_sha" in rules


class TestProseCitedSha:
    """The exact 3cs/1gg defect: a SHA claimed only in prose."""

    def test_unreachable_prose_sha_in_close_reason_is_caught(
        self, repo_root: Path
    ) -> None:
        bead = _bead(
            metadata={"evidence.artifact_path": "docs/investigations/x/writeup.md"},
            close_reason=(
                "Merged to codeprobe main @94d357c (--no-ff) 2026-06-16 per "
                "Stephanie decision 6. Published/reachable. git:94d357c"
            ),
        )
        result = _check(bead, reachable=set(), repo_root=repo_root)
        rules = [v.rule for v in result.violations]
        assert "unreachable_prose_sha" in rules
        assert any("94d357c" in v.detail for v in result.violations)

    def test_reachable_prose_sha_not_flagged(self, repo_root: Path) -> None:
        bead = _bead(
            metadata={
                "evidence.artifact_path": "docs/investigations/x/writeup.md",
                "evidence.doc_only": "true",
            },
            close_reason="Merged to main @cafe123, verified.",
        )
        result = _check(bead, reachable={"cafe123"}, repo_root=repo_root)
        assert not result.violations

    def test_prose_sha_already_checked_via_artifact_path_not_duplicated(
        self, repo_root: Path
    ) -> None:
        bead = _bead(
            metadata={"evidence.artifact_path": "git:abc1234"},
            close_reason="Merged @abc1234 to main.",
        )
        result = _check(bead, reachable={"abc1234"}, repo_root=repo_root)
        assert not result.violations
        # Checked once, not twice.
        assert result.shas_checked.count("abc1234") == 1

    def test_notes_field_also_scanned(self, repo_root: Path) -> None:
        bead = _bead(
            metadata={"evidence.artifact_path": "docs/x.md", "evidence.doc_only": "true"},
            notes="NOT merged -- mayor merges then closes with git:badc0de reachable",
        )
        result = _check(bead, reachable=set(), repo_root=repo_root)
        rules = [v.rule for v in result.violations]
        assert "unreachable_prose_sha" in rules


class TestDocOnlyEvidence:
    def test_doc_path_without_flag_is_flagged(self, repo_root: Path) -> None:
        bead = _bead(metadata={"evidence.artifact_path": "docs/investigations/x/writeup.md"})
        result = _check(bead, repo_root=repo_root)
        rules = [v.rule for v in result.violations]
        assert "doc_only_evidence" in rules

    def test_doc_path_with_explicit_opt_out_passes(self, repo_root: Path) -> None:
        bead = _bead(
            metadata={
                "evidence.artifact_path": "docs/investigations/x/writeup.md",
                "evidence.doc_only": "true",
            }
        )
        result = _check(bead, repo_root=repo_root)
        assert not result.violations

    def test_mixed_git_and_doc_path_not_flagged_as_doc_only(
        self, repo_root: Path
    ) -> None:
        # A comma-separated artifact_path with at least one git: entry is not
        # "doc-only" even without the opt-out flag.
        bead = _bead(
            metadata={
                "evidence.artifact_path": "tests/test_x.py,git:abc1234",
            }
        )
        result = _check(bead, reachable={"abc1234"}, repo_root=repo_root)
        assert not result.violations


class TestMissingEvidence:
    def test_closed_with_no_evidence_or_bypass_flagged(self, repo_root: Path) -> None:
        bead = _bead(metadata={})
        result = _check(bead, repo_root=repo_root)
        rules = [v.rule for v in result.violations]
        assert "missing_evidence" in rules

    def test_open_bead_with_no_evidence_not_flagged(self, repo_root: Path) -> None:
        bead = _bead(status="open", metadata={})
        result = _check(bead, repo_root=repo_root)
        assert not result.violations


class TestBypass:
    def test_legitimate_bypass_short_circuits_everything(self, repo_root: Path) -> None:
        bead = _bead(
            metadata={"gate_bypass": "superseded-by codeprobe-other"},
            close_reason="Merged @deadbee to main.",
        )
        result = _check(bead, reachable=set(), repo_root=repo_root)
        assert not result.violations
        assert result.bypass_legitimate

    def test_banned_future_tense_bypass_flagged(self, repo_root: Path) -> None:
        bead = _bead(metadata={"gate_bypass": "will merge as one squash later"})
        result = _check(bead, repo_root=repo_root)
        rules = [v.rule for v in result.violations]
        assert "banned_bypass" in rules


def test_extract_prose_shas_dedupes_and_preserves_order() -> None:
    text = "Merged @abc1234 to main. Also git:abc1234 and @deadbee."
    assert cbr.extract_prose_shas(text) == ["abc1234", "deadbee"]


def test_extract_prose_shas_empty_text() -> None:
    assert cbr.extract_prose_shas("") == []
