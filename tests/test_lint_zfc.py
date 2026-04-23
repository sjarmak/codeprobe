"""Unit tests for the ZFC boundary lint rule (INV3).

Drives ``scripts/lint_zfc.py`` at both the CLI surface (via subprocess
for ``--help`` and exit-code behavior) and the analyzer-function
surface (via in-process import for structural AST checks).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LINTER_PATH = REPO_ROOT / "scripts" / "lint_zfc.py"


# ---------------------------------------------------------------------------
# Module-loading helper — the linter lives under scripts/ (not inside the
# installed package) so we load it explicitly by path.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def linter():
    spec = importlib.util.spec_from_file_location("lint_zfc", LINTER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec_module so dataclass(__module__)
    # lookups during class construction can resolve. Required on 3.12+.
    sys.modules["lint_zfc"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Synthetic source fixtures.
#
# Each sample is a self-contained Python source string. The linter
# parses them via ast.parse — no imports resolve at parse time, so we
# don't need the fake ``call_claude``/``TaskMetadata`` to be real.
# ---------------------------------------------------------------------------

GOOD_SOURCE = textwrap.dedent(
    """
    def make_task():
        response = call_claude(prompt="judge this")
        difficulty_label = response.text.strip()
        meta.difficulty = difficulty_label  # OK: not a literal
        meta.expected_benefit = response.rating  # OK: not a literal

    def make_task_literal_after_model():
        call_claude(prompt="ok")
        meta.difficulty = "hard"  # OK: model was invoked first
    """
)

BAD_ATTR_ASSIGN_SOURCE = textwrap.dedent(
    """
    def make_task():
        meta.difficulty = "hard"  # BAD: no model invocation
    """
)

BAD_TASKMETADATA_KWARG_SOURCE = textwrap.dedent(
    """
    def make_task():
        return TaskMetadata(
            difficulty="medium",  # BAD: no model invocation
            expected_benefit="low",  # also BAD
        )
    """
)

MIXED_SOURCE = textwrap.dedent(
    """
    def good():
        call_claude(prompt="x")
        meta.difficulty = "easy"

    def bad():
        meta.difficulty = "easy"
    """
)


# ---------------------------------------------------------------------------
# Analyzer-function tests (structural correctness)
# ---------------------------------------------------------------------------


def test_good_sample_has_no_findings(linter):
    findings = linter.analyze_source(GOOD_SOURCE, "<good>")
    assert findings == [], f"Expected no findings; got {findings}"


def test_bad_attr_assign_is_flagged(linter):
    findings = linter.analyze_source(BAD_ATTR_ASSIGN_SOURCE, "<bad-attr>")
    assert len(findings) == 1
    assert findings[0].rule == "zfc-hardcoded-task-metadata"
    assert "difficulty" in findings[0].detail
    assert "'hard'" in findings[0].detail


def test_bad_taskmetadata_kwarg_is_flagged(linter):
    findings = linter.analyze_source(
        BAD_TASKMETADATA_KWARG_SOURCE, "<bad-kwarg>"
    )
    # Two violating kwargs on the TaskMetadata(...) call.
    assert len(findings) == 2
    details = sorted(f.detail for f in findings)
    assert any("difficulty" in d for d in details)
    assert any("expected_benefit" in d for d in details)


def test_mixed_source_flags_only_bad_function(linter):
    findings = linter.analyze_source(MIXED_SOURCE, "<mixed>")
    assert len(findings) == 1
    assert "'easy'" in findings[0].detail


def test_expected_prefix_attributes_are_detected(linter):
    source = textwrap.dedent(
        """
        def f():
            meta.expected_quality = "high"
        """
    )
    findings = linter.analyze_source(source, "<prefix>")
    assert len(findings) == 1
    assert "expected_quality" in findings[0].detail


def test_non_banned_strings_are_not_flagged(linter):
    source = textwrap.dedent(
        """
        def f():
            meta.difficulty = "bespoke"  # not in banned vocab
            meta.quality_band = "sub-acceptable"
        """
    )
    findings = linter.analyze_source(source, "<non-banned>")
    assert findings == []


def test_non_metadata_attribute_is_not_flagged(linter):
    source = textwrap.dedent(
        """
        def f():
            config.log_level = "low"  # not a TaskMetadata-shaped attr
            request.priority = "high"  # ditto
        """
    )
    findings = linter.analyze_source(source, "<non-meta>")
    assert findings == []


def test_nested_function_scopes_are_independent(linter):
    source = textwrap.dedent(
        """
        def outer():
            call_claude(prompt="hello")

            def inner():
                meta.difficulty = "hard"  # BAD: inner has no model call
        """
    )
    findings = linter.analyze_source(source, "<nested>")
    assert len(findings) == 1


# ---------------------------------------------------------------------------
# Allowlist tests
# ---------------------------------------------------------------------------


def test_allowlist_does_not_reverse_match_longer_entry(linter, tmp_path):
    """Regression: a longer allowlist entry path must NOT suppress a shorter finding.

    Batch A review (v0.6.0) found that ``is_suppressed`` had three OR
    arms for path matching, the third of which let an allowlist entry
    like ``evil/src/codeprobe/mining/extractor.py`` silently suppress
    findings at ``src/codeprobe/mining/extractor.py``. That arm was
    removed; this test pins the fix.
    """
    # Finding path is the canonical repo-relative one.
    finding_path = "src/codeprobe/mining/extractor.py"
    # Entry path is *longer* (has a prepended component) and should NOT match.
    entry_path = "evil/src/codeprobe/mining/extractor.py"

    finding = linter.Finding(
        file=finding_path,
        line=42,
        rule="zfc-hardcoded-task-metadata",
        detail="synthetic",
    )
    allowlist = (
        linter.AllowlistEntry(
            file=entry_path,
            line_start=1,
            line_end=10000,
            reason="should-not-match",
        ),
    )
    assert linter.is_suppressed(finding, allowlist) is False


def test_allowlist_still_matches_when_finding_is_longer_than_entry(
    linter, tmp_path
):
    """The legitimate case: absolute finding path ends with repo-relative entry."""
    finding = linter.Finding(
        file="/home/x/repo/src/codeprobe/mining/extractor.py",
        line=42,
        rule="zfc-hardcoded-task-metadata",
        detail="synthetic",
    )
    allowlist = (
        linter.AllowlistEntry(
            file="src/codeprobe/mining/extractor.py",
            line_start=1,
            line_end=10000,
            reason="legitimate suffix match",
        ),
    )
    assert linter.is_suppressed(finding, allowlist) is True


def test_allowlist_suppresses_finding(linter, tmp_path):
    source_path = tmp_path / "bad.py"
    source_path.write_text(BAD_ATTR_ASSIGN_SOURCE, encoding="utf-8")

    # Find the line number of the assignment in the synthetic source.
    raw_findings = linter.analyze_source(BAD_ATTR_ASSIGN_SOURCE, str(source_path))
    assert len(raw_findings) == 1
    bad_line = raw_findings[0].line

    allowlist = (
        linter.AllowlistEntry(
            file=str(source_path),
            line_start=bad_line,
            line_end=bad_line,
            reason="test",
        ),
    )

    finding = linter.Finding(
        file=str(source_path),
        line=bad_line,
        rule="zfc-hardcoded-task-metadata",
        detail="whatever",
    )
    assert linter.is_suppressed(finding, allowlist) is True


def test_allowlist_missing_file_returns_empty(linter, tmp_path):
    missing = tmp_path / "nope.toml"
    assert linter.load_allowlist(missing) == ()


def test_allowlist_parses_real_file(linter):
    allowlist_path = REPO_ROOT / "scripts" / "lint_zfc.allowlist.toml"
    entries = linter.load_allowlist(allowlist_path)
    # At least the four CLAUDE.md known violations must be present.
    assert len(entries) >= 4
    files = {e.file for e in entries}
    assert "src/codeprobe/mining/extractor.py" in files
    assert "src/codeprobe/assess/heuristics.py" in files
    assert "src/codeprobe/cli/mine_cmd.py" in files
    assert "src/codeprobe/mining/org_scale_families.py" in files


def test_allowlist_malformed_raises(linter, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text(
        '[[entry]]\nfile = "x.py"\n# missing line_start/line_end\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        linter.load_allowlist(bad)


# ---------------------------------------------------------------------------
# CLI-level tests (subprocess)
# ---------------------------------------------------------------------------


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(LINTER_PATH), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_help_exits_zero():
    result = _run_cli("--help")
    assert result.returncode == 0, result.stderr
    assert "lint_zfc" in result.stdout.lower()


def test_cli_clean_directory_exits_zero(tmp_path):
    # Empty .py file produces no findings.
    (tmp_path / "clean.py").write_text("x = 1\n", encoding="utf-8")
    result = _run_cli(str(tmp_path))
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_cli_reports_violation_with_machine_parseable_line(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(BAD_ATTR_ASSIGN_SOURCE, encoding="utf-8")

    # Point to a non-existent allowlist so the default doesn't suppress.
    empty_allowlist = tmp_path / "empty.toml"
    result = _run_cli(
        "--allowlist",
        str(empty_allowlist),
        str(tmp_path),
    )
    assert result.returncode == 1, result.stderr
    # First stderr line should match file:line:rule pattern.
    first_line = result.stderr.strip().splitlines()[0]
    parts = first_line.split(":", 3)
    assert len(parts) >= 3, first_line
    assert parts[2].startswith("zfc-"), first_line


def test_cli_allowlist_suppresses_violation(tmp_path, linter):
    bad = tmp_path / "bad.py"
    bad.write_text(BAD_ATTR_ASSIGN_SOURCE, encoding="utf-8")

    # Compute the offending line by running the analyzer in-process.
    raw_findings = linter.analyze_source(BAD_ATTR_ASSIGN_SOURCE, str(bad))
    bad_line = raw_findings[0].line

    allowlist_path = tmp_path / "allow.toml"
    allowlist_path.write_text(
        textwrap.dedent(
            f"""
            [[entry]]
            file = "{bad.as_posix()}"
            line_start = {bad_line}
            line_end = {bad_line}
            reason = "test"
            """
        ),
        encoding="utf-8",
    )

    result = _run_cli(
        "--allowlist",
        str(allowlist_path),
        str(tmp_path),
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


def test_cli_mining_directory_is_clean(tmp_path):
    """Running against the real mining/ dir with the shipped allowlist
    must exit 0 — the four known violations are allowlisted and no new
    ones should have crept in."""
    mining_dir = REPO_ROOT / "src" / "codeprobe" / "mining"
    if not mining_dir.exists():
        pytest.skip("mining dir not present in this checkout")
    result = _run_cli(str(mining_dir))
    # If the linter reports anything, the stderr output tells us which
    # file:line:rule triple tripped — fail loudly with that context.
    assert result.returncode == 0, (
        f"Expected clean run; stderr was:\n{result.stderr}"
    )
