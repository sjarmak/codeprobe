"""Tests for the acceptance verifier — eval_mode_required skip behavior.

Covers the Verifier's behavior when a criterion declares
``eval_mode_required`` and the current eval mode does not match.
"""

from __future__ import annotations

from pathlib import Path

from acceptance.verify import RESULT_SKIP, Verifier

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_minimal_manifest(tmp_path: Path, criteria_toml: str) -> Path:
    """Write a minimal criteria.toml and return its path."""
    p = tmp_path / "acceptance" / "criteria.toml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(criteria_toml, encoding="utf-8")
    return p


def _make_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


# ---------------------------------------------------------------------------
# eval_mode_required → skip when mode does not match
# ---------------------------------------------------------------------------


def test_verifier_skips_when_eval_mode_required_not_met(tmp_path: Path) -> None:
    """Criterion with eval_mode_required='full' is skipped in 'structural' mode."""
    toml = """
[[criterion]]
id = "NEED-FULL-001"
description = "needs full run"
tier = "structural"
check_type = "import_equals"
severity = "high"
prd_source = "docs/prd/x.md"
eval_mode_required = "full"
[criterion.params]
module = "builtins"
symbol = "True"
expected = true
"""
    manifest = _write_minimal_manifest(tmp_path, toml)
    workspace = _make_workspace(tmp_path)

    # Without eval_mode (default=None), criterion with eval_mode_required="full"
    # is skipped because None != "full"
    verifier_default = Verifier(manifest, project_root=tmp_path)
    verdict_default = verifier_default.run(workspace)
    assert verdict_default["skip_count"] == 1
    assert verdict_default["pass_count"] == 0

    # With eval_mode="structural", criterion should also be skipped
    verifier = Verifier(manifest, project_root=tmp_path, eval_mode="structural")
    verdict = verifier.run(workspace)
    assert verdict["skip_count"] == 1
    # Find the skip evidence
    results = verdict.get("results", [])
    # The verdict dict doesn't store individual results, but we can check counts
    assert verdict["pass_count"] == 0
    assert verdict["skip_count"] == 1


def test_verifier_runs_when_eval_mode_matches(tmp_path: Path) -> None:
    """Criterion with eval_mode_required='full' runs when mode is 'full'."""
    toml = """
[[criterion]]
id = "NEED-FULL-001"
description = "needs full run"
tier = "structural"
check_type = "import_equals"
severity = "high"
prd_source = "docs/prd/x.md"
eval_mode_required = "full"
[criterion.params]
module = "builtins"
symbol = "True"
expected = true
"""
    manifest = _write_minimal_manifest(tmp_path, toml)
    workspace = _make_workspace(tmp_path)

    verifier = Verifier(manifest, project_root=tmp_path, eval_mode="full")
    verdict = verifier.run(workspace)
    assert verdict["pass_count"] == 1
    assert verdict["skip_count"] == 0


def test_verifier_no_eval_mode_required_always_runs(tmp_path: Path) -> None:
    """Criterion without eval_mode_required runs regardless of eval_mode."""
    toml = """
[[criterion]]
id = "ALWAYS-001"
description = "always runs"
tier = "structural"
check_type = "import_equals"
severity = "high"
prd_source = "docs/prd/x.md"
[criterion.params]
module = "builtins"
symbol = "True"
expected = true
"""
    manifest = _write_minimal_manifest(tmp_path, toml)
    workspace = _make_workspace(tmp_path)

    verifier = Verifier(manifest, project_root=tmp_path, eval_mode="structural")
    verdict = verifier.run(workspace)
    assert verdict["pass_count"] == 1
    assert verdict["skip_count"] == 0


def test_verifier_default_eval_mode_is_none(tmp_path: Path) -> None:
    """When no eval_mode is passed, Verifier.eval_mode is None."""
    toml = """
[[criterion]]
id = "X-001"
description = "any"
tier = "structural"
check_type = "import_equals"
severity = "high"
prd_source = "docs/prd/x.md"
[criterion.params]
module = "builtins"
symbol = "True"
expected = true
"""
    manifest = _write_minimal_manifest(tmp_path, toml)
    verifier = Verifier(manifest, project_root=tmp_path)
    assert verifier.eval_mode is None


def test_verifier_skip_evidence_mentions_eval_mode(tmp_path: Path) -> None:
    """Skip evidence should clearly indicate the mode mismatch."""
    toml = """
[[criterion]]
id = "NEED-FULL-001"
description = "needs full run"
tier = "structural"
check_type = "import_equals"
severity = "high"
prd_source = "docs/prd/x.md"
eval_mode_required = "full"
[criterion.params]
module = "builtins"
symbol = "True"
expected = true
"""
    manifest = _write_minimal_manifest(tmp_path, toml)
    workspace = _make_workspace(tmp_path)

    verifier = Verifier(manifest, project_root=tmp_path, eval_mode="structural")
    # Access internal method to check evidence
    criterion = verifier.criteria[0]
    result = verifier._check_eval_mode(criterion)
    assert result is not None
    assert result.result == RESULT_SKIP
    assert "eval_mode" in result.evidence
    assert "full" in result.evidence
