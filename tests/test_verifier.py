"""Tests for :mod:`acceptance.verify`.

These tests use a minimal, self-contained TOML manifest so they don't depend
on the real ``acceptance/criteria.toml`` shifting underneath them. The real
manifest is exercised separately via a smoke test at the bottom of the file
that just asserts the verifier can load it and produce a structurally valid
verdict.

The tests cover the explicit acceptance criteria from the work-unit brief:

- ``Verifier('criteria.toml')`` constructs successfully.
- Running against a workspace produces a parseable ``verdict.json``.
- The verdict contains ``pass_count``, ``fail_count``, ``skip_count``,
  ``evaluated_pct`` per tier, ``all_pass``, and a ``failures`` list.
- Every failure entry has ``criterion_id``, ``evidence``, and ``severity``.
- A tier with <80% evaluated criteria forces ``status == 'INCOMPLETE'``.
- Structural criteria run without a workspace (Python introspection).
- Behavioral criteria consume captured CLI outputs in the workspace
  (exit codes, stdout substrings, file existence).
- Canary detection passes when the sentinel UUID is present in another
  workspace file and fails when it isn't.
"""

from __future__ import annotations

import importlib
import json
import sys
import textwrap
from pathlib import Path

import pytest

from acceptance.verify import (
    CANARY_FILENAME,
    RESULT_FAIL,
    RESULT_PASS,
    RESULT_SKIP,
    STATUS_EVALUATED,
    STATUS_INCOMPLETE,
    Verifier,
    _jsonpath_select,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def structural_only_manifest(tmp_path: Path) -> Path:
    """A manifest with only structural checks that all pass against stdlib."""
    manifest = tmp_path / "criteria.toml"
    manifest.write_text(textwrap.dedent("""
            [[criterion]]
            id = "STRUCT-JSON-LOADS"
            description = "json.loads exists"
            tier = "structural"
            check_type = "dataclass_has_fields"
            severity = "high"
            prd_source = "fake.md#x"
            [criterion.params]
            module = "acceptance.loader"
            symbol = "Criterion"
            required_fields = ["id", "description", "tier"]
            """).strip())
    return manifest


@pytest.fixture()
def mixed_manifest(tmp_path: Path) -> Path:
    """Manifest with one criterion per tier for threshold tests."""
    manifest = tmp_path / "criteria.toml"
    manifest.write_text(textwrap.dedent("""
            [[criterion]]
            id = "STRUCT-1"
            description = "Criterion dataclass has expected fields"
            tier = "structural"
            check_type = "dataclass_has_fields"
            severity = "high"
            prd_source = "fake.md#x"
            [criterion.params]
            module = "acceptance.loader"
            symbol = "Criterion"
            required_fields = ["id", "tier", "check_type"]

            [[criterion]]
            id = "BEH-1"
            description = "captured exit code is zero"
            tier = "behavioral"
            check_type = "cli_exit_code"
            severity = "high"
            prd_source = "fake.md#y"
            [criterion.params]
            expected_exit = 0

            [[criterion]]
            id = "STAT-1"
            description = "results has at least 1 entry"
            tier = "statistical"
            check_type = "json_count_ge"
            severity = "high"
            prd_source = "fake.md#z"
            [criterion.params]
            source = "results.json"
            jsonpath = "$.completed_tasks"
            min_count = 1
            """).strip())
    return manifest


@pytest.fixture()
def full_workspace(tmp_path: Path) -> Path:
    """A workspace that satisfies every criterion in ``mixed_manifest``."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "BEH-1.exit").write_text("0\n")
    (ws / "results.json").write_text(
        json.dumps({"completed_tasks": [{"id": "t1", "cost_usd": 0.1}]})
    )
    return ws


# ---------------------------------------------------------------------------
# Construction and basic run
# ---------------------------------------------------------------------------


def test_verifier_constructs_from_real_manifest() -> None:
    """Acceptance criterion: python -c 'from acceptance.verify import Verifier;
    v = Verifier("acceptance/criteria.toml")' succeeds.
    """
    v = Verifier("acceptance/criteria.toml")
    assert len(v.criteria) >= 25


def test_verifier_rejects_missing_manifest(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        Verifier(tmp_path / "does-not-exist.toml")


def test_run_against_empty_workspace_produces_valid_verdict(
    mixed_manifest: Path, tmp_path: Path
) -> None:
    v = Verifier(mixed_manifest)
    verdict = v.run(tmp_path / "ws")
    # Required keys present.
    for key in (
        "pass_count",
        "fail_count",
        "skip_count",
        "total_criteria",
        "evaluated_pct",
        "tier_counts",
        "status",
        "all_pass",
        "failures",
    ):
        assert key in verdict, f"missing key {key}"
    # Counts add up to total.
    assert (
        verdict["pass_count"] + verdict["fail_count"] + verdict["skip_count"]
        == verdict["total_criteria"]
    )
    # Per-tier evaluated_pct has an entry for every allowed tier.
    assert set(verdict["evaluated_pct"]) == {
        "structural",
        "behavioral",
        "statistical",
    }


def test_verdict_is_valid_json_roundtrip(
    mixed_manifest: Path, tmp_path: Path, full_workspace: Path
) -> None:
    v = Verifier(mixed_manifest)
    verdict = v.run(full_workspace)
    out = v.write_verdict(verdict, tmp_path / "verdict.json")
    assert out.is_file()
    parsed = json.loads(out.read_text())
    assert parsed["total_criteria"] == verdict["total_criteria"]


# ---------------------------------------------------------------------------
# Tier thresholds
# ---------------------------------------------------------------------------


def test_incomplete_when_tier_below_threshold(
    mixed_manifest: Path, tmp_path: Path
) -> None:
    """With no workspace artifacts, behavioral + statistical tiers have
    0% evaluated → verdict must be INCOMPLETE and all_pass must be False.
    """
    v = Verifier(mixed_manifest)
    verdict = v.run(tmp_path / "ws")
    assert verdict["status"] == STATUS_INCOMPLETE
    assert verdict["all_pass"] is False
    assert verdict["evaluated_pct"]["statistical"] == 0.0
    assert verdict["evaluated_pct"]["behavioral"] == 0.0
    assert verdict["evaluated_pct"]["structural"] == 100.0


def test_evaluated_status_when_all_tiers_meet_threshold(
    mixed_manifest: Path, full_workspace: Path
) -> None:
    v = Verifier(mixed_manifest)
    verdict = v.run(full_workspace)
    # Every criterion evaluated (pass or fail), so each tier is at 100%.
    assert verdict["evaluated_pct"]["structural"] == 100.0
    assert verdict["evaluated_pct"]["behavioral"] == 100.0
    assert verdict["evaluated_pct"]["statistical"] == 100.0
    assert verdict["status"] == STATUS_EVALUATED
    assert verdict["fail_count"] == 0
    assert verdict["all_pass"] is True


def test_all_pass_false_when_fail_count_nonzero(
    mixed_manifest: Path, tmp_path: Path
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    # exit code non-zero → BEH-1 fails.
    (ws / "BEH-1.exit").write_text("1\n")
    (ws / "results.json").write_text(json.dumps({"completed_tasks": [{"id": "t1"}]}))
    v = Verifier(mixed_manifest)
    verdict = v.run(ws)
    assert verdict["status"] == STATUS_EVALUATED
    assert verdict["fail_count"] >= 1
    assert verdict["all_pass"] is False


def test_failures_carry_required_fields(mixed_manifest: Path, tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "BEH-1.exit").write_text("2\n")
    (ws / "results.json").write_text(json.dumps({"completed_tasks": []}))
    v = Verifier(mixed_manifest)
    verdict = v.run(ws)
    assert len(verdict["failures"]) >= 1
    for failure in verdict["failures"]:
        assert "criterion_id" in failure
        assert "evidence" in failure
        assert "severity" in failure
        assert isinstance(failure["evidence"], str)
        assert failure["evidence"]


# ---------------------------------------------------------------------------
# Structural handlers
# ---------------------------------------------------------------------------


def test_structural_checks_need_no_workspace(
    structural_only_manifest: Path, tmp_path: Path
) -> None:
    v = Verifier(structural_only_manifest)
    verdict = v.run(tmp_path / "unused")
    assert verdict["pass_count"] == 1
    assert verdict["fail_count"] == 0
    assert verdict["status"] == STATUS_EVALUATED
    assert verdict["all_pass"] is True


def test_import_equals_handler(tmp_path: Path) -> None:
    manifest = tmp_path / "criteria.toml"
    manifest.write_text(textwrap.dedent("""
            [[criterion]]
            id = "IMP-OK"
            description = "known constant equals expected"
            tier = "structural"
            check_type = "import_equals"
            severity = "high"
            prd_source = "fake.md#x"
            [criterion.params]
            module = "acceptance.verify"
            symbol = "MIN_TIER_EVALUATED_PCT"
            expected = 80.0

            [[criterion]]
            id = "IMP-BAD"
            description = "wrong expected"
            tier = "structural"
            check_type = "import_equals"
            severity = "high"
            prd_source = "fake.md#x"
            [criterion.params]
            module = "acceptance.verify"
            symbol = "MIN_TIER_EVALUATED_PCT"
            expected = 50.0
            """).strip())
    v = Verifier(manifest)
    verdict = v.run(tmp_path / "ws")
    assert verdict["pass_count"] == 1
    assert verdict["fail_count"] == 1


# ---------------------------------------------------------------------------
# Subprocess-mode introspection (codeprobe-zqmr): import_equals /
# dataclass_has_fields must resolve imports in a *staged* interpreter, not
# whichever interpreter is running the verifier, when python_interpreter is
# set. These tests prove the smoke does not depend on the caller interpreter
# by making a fake module importable ONLY via an env var (PYTHONPATH) that is
# set *after* this process's sys.path was already computed — so the caller
# genuinely cannot import it in-process, while a freshly-spawned subprocess
# (standing in for a staged venv) picks it up at interpreter startup.
# ---------------------------------------------------------------------------


@pytest.fixture()
def staged_fake_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A module importable only by a subprocess that inherits PYTHONPATH.

    Confirms up front that the *caller* cannot import it, then arranges for
    a child ``python -c`` process to succeed via PYTHONPATH — simulating a
    staged venv without needing to actually build one.
    """
    staged_dir = tmp_path / "staged_pkgs"
    staged_dir.mkdir()
    (staged_dir / "codeprobe_zqmr_fakemod.py").write_text(
        "ANSWER = 42\n\n\nclass Widget:\n    x: int\n    y: str\n"
    )
    with pytest.raises(ImportError):
        importlib.import_module("codeprobe_zqmr_fakemod")
    monkeypatch.setenv("PYTHONPATH", str(staged_dir))
    return staged_dir


def test_import_equals_subprocess_mode_passes_via_staged_interpreter(
    tmp_path: Path, staged_fake_module: Path
) -> None:
    manifest = tmp_path / "criteria.toml"
    manifest.write_text(textwrap.dedent("""
            [[criterion]]
            id = "IMP-STAGED-OK"
            description = "constant only importable in the staged interpreter"
            tier = "structural"
            check_type = "import_equals"
            severity = "high"
            prd_source = "fake.md#x"
            [criterion.params]
            module = "codeprobe_zqmr_fakemod"
            symbol = "ANSWER"
            expected = 42
            """).strip())
    v = Verifier(manifest, python_interpreter=Path(sys.executable))
    verdict = v.run(tmp_path / "ws")
    assert verdict["pass_count"] == 1, verdict["failures"]
    assert verdict["fail_count"] == 0


def test_dataclass_has_fields_subprocess_mode_pass_and_fail(
    tmp_path: Path, staged_fake_module: Path
) -> None:
    manifest = tmp_path / "criteria.toml"
    manifest.write_text(textwrap.dedent("""
            [[criterion]]
            id = "DC-STAGED-OK"
            description = "Widget has expected fields, checked in staged interpreter"
            tier = "structural"
            check_type = "dataclass_has_fields"
            severity = "high"
            prd_source = "fake.md#x"
            [criterion.params]
            module = "codeprobe_zqmr_fakemod"
            symbol = "Widget"
            required_fields = ["x", "y"]

            [[criterion]]
            id = "DC-STAGED-MISSING"
            description = "Widget is missing a field that was never declared"
            tier = "structural"
            check_type = "dataclass_has_fields"
            severity = "high"
            prd_source = "fake.md#x"
            [criterion.params]
            module = "codeprobe_zqmr_fakemod"
            symbol = "Widget"
            required_fields = ["x", "z"]
            """).strip())
    v = Verifier(manifest, python_interpreter=Path(sys.executable))
    verdict = v.run(tmp_path / "ws")
    assert verdict["pass_count"] == 1, verdict["failures"]
    assert verdict["fail_count"] == 1


def test_import_equals_subprocess_mode_missing_symbol_fails(
    tmp_path: Path, staged_fake_module: Path
) -> None:
    manifest = tmp_path / "criteria.toml"
    manifest.write_text(textwrap.dedent("""
            [[criterion]]
            id = "IMP-STAGED-MISSING-SYMBOL"
            description = "symbol does not exist on the staged module"
            tier = "structural"
            check_type = "import_equals"
            severity = "high"
            prd_source = "fake.md#x"
            [criterion.params]
            module = "codeprobe_zqmr_fakemod"
            symbol = "NOPE"
            expected = 1
            """).strip())
    v = Verifier(manifest, python_interpreter=Path(sys.executable))
    verdict = v.run(tmp_path / "ws")
    assert verdict["fail_count"] == 1
    assert "not defined" in verdict["failures"][0]["evidence"]


def test_import_equals_subprocess_mode_import_error_skips(tmp_path: Path) -> None:
    """No PYTHONPATH trick here — the module genuinely does not exist
    anywhere, so even the staged interpreter must skip (not fail)."""
    manifest = tmp_path / "criteria.toml"
    manifest.write_text(textwrap.dedent("""
            [[criterion]]
            id = "IMP-STAGED-NO-MODULE"
            description = "module does not exist anywhere"
            tier = "structural"
            check_type = "import_equals"
            severity = "high"
            prd_source = "fake.md#x"
            [criterion.params]
            module = "codeprobe_zqmr_totally_absent_module"
            symbol = "ANSWER"
            expected = 1
            """).strip())
    v = Verifier(manifest, python_interpreter=Path(sys.executable))
    verdict = v.run(tmp_path / "ws")
    assert verdict["skip_count"] == 1
    assert verdict["pass_count"] == 0
    assert verdict["fail_count"] == 0


def test_import_equals_subprocess_crash_skips_with_evidence(tmp_path: Path) -> None:
    """A ``python_interpreter`` that isn't Python at all (crashes on any
    invocation) must be a loud skip, never a silent pass."""
    manifest = tmp_path / "criteria.toml"
    manifest.write_text(textwrap.dedent("""
            [[criterion]]
            id = "IMP-CRASHED-INTERPRETER"
            description = "the staged interpreter itself is broken"
            tier = "structural"
            check_type = "import_equals"
            severity = "high"
            prd_source = "fake.md#x"
            [criterion.params]
            module = "codeprobe_zqmr_fakemod"
            symbol = "ANSWER"
            expected = 42
            """).strip())
    v = Verifier(manifest, python_interpreter=Path("/bin/false"))
    verdict = v.run(tmp_path / "ws")
    assert verdict["skip_count"] == 1
    assert verdict["pass_count"] == 0
    assert verdict["fail_count"] == 0


def test_import_equals_python_interpreter_none_preserves_in_process_behavior(
    tmp_path: Path,
) -> None:
    """Default (``python_interpreter=None``) must be byte-for-byte the
    historical in-process path — no subprocess spawned at all."""
    manifest = tmp_path / "criteria.toml"
    manifest.write_text(textwrap.dedent("""
            [[criterion]]
            id = "IMP-INPROCESS"
            description = "known constant equals expected, in-process"
            tier = "structural"
            check_type = "import_equals"
            severity = "high"
            prd_source = "fake.md#x"
            [criterion.params]
            module = "acceptance.verify"
            symbol = "MIN_TIER_EVALUATED_PCT"
            expected = 80.0
            """).strip())
    v = Verifier(manifest)
    assert v.python_interpreter is None
    verdict = v.run(tmp_path / "ws")
    assert verdict["pass_count"] == 1
    assert verdict["fail_count"] == 0


def test_regex_present_handler(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "acceptance").mkdir(parents=True)
    (project / "acceptance" / "src.py").write_text("hello world\n")
    manifest = project / "acceptance" / "criteria.toml"
    manifest.write_text(textwrap.dedent("""
            [[criterion]]
            id = "REG-HIT"
            description = "file contains hello"
            tier = "structural"
            check_type = "regex_present"
            severity = "high"
            prd_source = "fake.md#x"
            [criterion.params]
            file = "acceptance/src.py"
            pattern = "hello"

            [[criterion]]
            id = "REG-MISS"
            description = "file contains goodbye"
            tier = "structural"
            check_type = "regex_present"
            severity = "high"
            prd_source = "fake.md#x"
            [criterion.params]
            file = "acceptance/src.py"
            pattern = "goodbye"
            """).strip())
    v = Verifier(manifest, project_root=project)
    verdict = v.run(tmp_path / "ws")
    failures = {f["criterion_id"] for f in verdict["failures"]}
    assert "REG-MISS" in failures
    assert "REG-HIT" not in failures


# ---------------------------------------------------------------------------
# Behavioral handlers
# ---------------------------------------------------------------------------


def test_cli_exit_code_pass_fail(mixed_manifest: Path, tmp_path: Path) -> None:
    ws_pass = tmp_path / "pass"
    ws_pass.mkdir()
    (ws_pass / "BEH-1.exit").write_text("0")

    ws_fail = tmp_path / "fail"
    ws_fail.mkdir()
    (ws_fail / "BEH-1.exit").write_text("7")

    v = Verifier(mixed_manifest)
    beh_pass = next(
        r for r in _criterion_results(v, ws_pass) if r["criterion_id"] == "BEH-1"
    )
    beh_fail = next(
        r for r in _criterion_results(v, ws_fail) if r["criterion_id"] == "BEH-1"
    )
    assert beh_pass["result"] == RESULT_PASS
    assert beh_fail["result"] == RESULT_FAIL


def test_cli_exit_code_missing_artifact_skips(
    mixed_manifest: Path, tmp_path: Path
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    v = Verifier(mixed_manifest)
    beh = next(r for r in _criterion_results(v, ws) if r["criterion_id"] == "BEH-1")
    assert beh["result"] == RESULT_SKIP
    assert "missing" in beh["evidence"]


def test_cli_writes_file_handler(tmp_path: Path) -> None:
    manifest = tmp_path / "criteria.toml"
    manifest.write_text(textwrap.dedent("""
            [[criterion]]
            id = "WF-OK"
            description = "cli wrote the expected file"
            tier = "behavioral"
            check_type = "cli_writes_file"
            severity = "high"
            prd_source = "fake.md#x"
            [criterion.params]
            expected_path = ".codeprobe/experiment.json"
            """).strip())
    ws = tmp_path / "ws"
    (ws / ".codeprobe").mkdir(parents=True)
    (ws / ".codeprobe" / "experiment.json").write_text("{}")
    v = Verifier(manifest)
    verdict = v.run(ws)
    assert verdict["pass_count"] == 1

    # And a missing file → fail.
    ws2 = tmp_path / "ws2"
    ws2.mkdir()
    verdict2 = v.run(ws2)
    assert verdict2["fail_count"] == 1


def test_cli_stdout_contains_handler(tmp_path: Path) -> None:
    manifest = tmp_path / "criteria.toml"
    manifest.write_text(textwrap.dedent("""
            [[criterion]]
            id = "STDOUT-HIT"
            description = "stdout mentions validate"
            tier = "behavioral"
            check_type = "cli_stdout_contains"
            severity = "high"
            prd_source = "fake.md#x"
            [criterion.params]
            must_contain = "task-001"
            """).strip())
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "STDOUT-HIT.stdout").write_text("discovered task-001 successfully\n")
    v = Verifier(manifest)
    verdict = v.run(ws)
    assert verdict["pass_count"] == 1


# ---------------------------------------------------------------------------
# Statistical handlers
# ---------------------------------------------------------------------------


def test_json_field_not_null_pass_and_fail(tmp_path: Path) -> None:
    manifest = tmp_path / "criteria.toml"
    manifest.write_text(textwrap.dedent("""
            [[criterion]]
            id = "JSON-NN"
            description = "cost_source never null"
            tier = "statistical"
            check_type = "json_field_not_null"
            severity = "critical"
            prd_source = "fake.md#x"
            [criterion.params]
            source = "results.json"
            jsonpath = "$.completed_tasks[*].cost_source"
            forbid_values = ["", "none"]
            """).strip())
    v = Verifier(manifest)

    ws_good = tmp_path / "good"
    ws_good.mkdir()
    (ws_good / "results.json").write_text(
        json.dumps(
            {
                "completed_tasks": [
                    {"cost_source": "cli"},
                    {"cost_source": "envelope"},
                ]
            }
        )
    )
    verdict_good = v.run(ws_good)
    assert verdict_good["pass_count"] == 1

    ws_bad = tmp_path / "bad"
    ws_bad.mkdir()
    (ws_bad / "results.json").write_text(
        json.dumps({"completed_tasks": [{"cost_source": "cli"}, {"cost_source": None}]})
    )
    verdict_bad = v.run(ws_bad)
    assert verdict_bad["fail_count"] == 1


def test_json_count_ge_handler(mixed_manifest: Path, tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "BEH-1.exit").write_text("0")
    (ws / "results.json").write_text(json.dumps({"completed_tasks": []}))
    v = Verifier(mixed_manifest)
    verdict = v.run(ws)
    # STAT-1 requires min_count=1 but we have 0 → fail.
    fail_ids = {f["criterion_id"] for f in verdict["failures"]}
    assert "STAT-1" in fail_ids


# ---------------------------------------------------------------------------
# Canary detection
# ---------------------------------------------------------------------------


def _canary_manifest(path: Path) -> Path:
    path.write_text(textwrap.dedent("""
            [[criterion]]
            id = "CAN-1"
            description = "canary uuid appears in workspace"
            tier = "statistical"
            check_type = "canary_detect"
            severity = "high"
            prd_source = "fake.md#x"
            [criterion.params]
            """).strip())
    return path


def test_canary_skipped_when_file_missing(tmp_path: Path) -> None:
    manifest = _canary_manifest(tmp_path / "criteria.toml")
    ws = tmp_path / "ws"
    ws.mkdir()
    v = Verifier(manifest)
    verdict = v.run(ws)
    assert verdict["skip_count"] == 1
    assert verdict["pass_count"] == 0
    assert verdict["fail_count"] == 0


def test_canary_passes_when_uuid_present(tmp_path: Path) -> None:
    manifest = _canary_manifest(tmp_path / "criteria.toml")
    ws = tmp_path / "ws"
    ws.mkdir()
    uuid = "11111111-2222-3333-4444-555555555555"
    (ws / CANARY_FILENAME).write_text(uuid)
    (ws / "agent.log").write_text(f"started run, token={uuid}, done\n")
    v = Verifier(manifest)
    verdict = v.run(ws)
    assert verdict["pass_count"] == 1


def test_canary_fails_when_uuid_absent(tmp_path: Path) -> None:
    manifest = _canary_manifest(tmp_path / "criteria.toml")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / CANARY_FILENAME).write_text("abc-uuid")
    (ws / "agent.log").write_text("no sentinel here\n")
    v = Verifier(manifest)
    verdict = v.run(ws)
    assert verdict["fail_count"] == 1


# ---------------------------------------------------------------------------
# JSONPath selector unit tests
# ---------------------------------------------------------------------------


def test_jsonpath_simple_field() -> None:
    data = {"a": {"b": 42}}
    assert _jsonpath_select(data, "$.a.b") == 42


def test_jsonpath_list_star() -> None:
    data = {"xs": [{"k": 1}, {"k": 2}, {"k": 3}]}
    assert _jsonpath_select(data, "$.xs[*].k") == [1, 2, 3]


def test_jsonpath_wildcard_dict() -> None:
    data = {"jobs": {"a": {"runs-on": "ubuntu"}, "b": {"runs-on": "ubuntu"}}}
    assert _jsonpath_select(data, "$.jobs.*.runs-on") == ["ubuntu", "ubuntu"]


def test_jsonpath_missing_returns_none() -> None:
    data = {"a": 1}
    assert _jsonpath_select(data, "$.nonexistent") is None


# ---------------------------------------------------------------------------
# Skip semantics for unsupported check_types
# ---------------------------------------------------------------------------


def test_unsupported_check_type_skipped(tmp_path: Path) -> None:
    manifest = tmp_path / "criteria.toml"
    manifest.write_text(textwrap.dedent("""
            [[criterion]]
            id = "UNSUP"
            description = "made-up check"
            tier = "behavioral"
            check_type = "does_not_exist"
            severity = "low"
            prd_source = "fake.md#x"
            [criterion.params]
            """).strip())
    v = Verifier(manifest)
    verdict = v.run(tmp_path / "ws")
    assert verdict["skip_count"] == 1
    assert verdict["status"] == STATUS_EVALUATED  # skip reduces effective_total to 0


# ---------------------------------------------------------------------------
# Smoke test: real manifest loads and evaluates
# ---------------------------------------------------------------------------


def test_real_manifest_smoke(tmp_path: Path) -> None:
    v = Verifier("acceptance/criteria.toml")
    verdict = v.run(tmp_path / "ws")
    # verdict.json should be writable.
    out = v.write_verdict(verdict, tmp_path / "verdict.json")
    parsed = json.loads(out.read_text())
    assert parsed["total_criteria"] == len(v.criteria)
    assert parsed["total_criteria"] >= 25
    assert set(parsed["evaluated_pct"]) == {
        "structural",
        "behavioral",
        "statistical",
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _criterion_results(v: Verifier, workspace: Path) -> list[dict[str, str]]:
    """Re-run criterion evaluation and return a list[dict] for easy lookups."""
    verdict = v.run(workspace)
    # The verdict only records failures; for tests we want every per-criterion
    # result, so we walk the results manually via the verifier's internals.
    # This mirrors run() but is test-scoped.
    out: list[dict[str, str]] = []
    ws = Path(workspace).resolve()
    ws.mkdir(parents=True, exist_ok=True)
    for criterion in v.criteria:
        handler = v._handlers().get(criterion.check_type)
        if handler is None:
            out.append(
                {
                    "criterion_id": criterion.id,
                    "result": RESULT_SKIP,
                    "evidence": "unsupported",
                }
            )
            continue
        res = handler(v, criterion, ws)
        out.append(
            {
                "criterion_id": res.criterion_id,
                "result": res.result,
                "evidence": res.evidence,
            }
        )
    # Silence unused verdict.
    _ = verdict
    return out
