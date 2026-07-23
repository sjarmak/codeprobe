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
import subprocess
import sys
import textwrap
import venv
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
    _canonicalize_for_import_equals,
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
# set. These tests build a real (bare, no-pip) venv and install the fake
# module into ITS OWN site-packages — the actual mechanism by which a staged
# release venv exposes a package — rather than simulating "staged" via a
# PYTHONPATH shadow. A PYTHONPATH shadow is exactly what production isolation
# (``-I``) must ignore, so using one as the fixture's mechanism would make
# the fixture and the isolation fix contradict each other and the tests
# would stop proving anything about staged-venv resolution.
# ---------------------------------------------------------------------------


def _staged_python(venv_dir: Path) -> Path:
    return venv_dir / "bin" / "python3"


def _staged_site_packages(venv_dir: Path) -> Path:
    completed = subprocess.run(
        [
            str(_staged_python(venv_dir)),
            "-I",
            "-c",
            "import json, sysconfig; print(json.dumps(sysconfig.get_paths()['purelib']))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(json.loads(completed.stdout))


@pytest.fixture(scope="module")
def staged_venv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real, throwaway venv standing in for a staged release venv.

    Bare venv creation (no pip) is cheap, so building one per test module is
    fast while still exercising an actual separate interpreter with its own
    ``site-packages``.
    """
    venv_dir = tmp_path_factory.mktemp("staged-venv")
    venv.create(venv_dir, with_pip=False, clear=True)
    return venv_dir


@pytest.fixture()
def staged_fake_module(staged_venv: Path) -> Path:
    """Install a fake module into the staged venv's own site-packages.

    Confirms up front that *this* process cannot import it (proving it is
    only reachable through the staged venv, not leaked in some other way),
    installs it into the staged venv's real site-packages, and yields the
    staged venv's python interpreter path for use as
    ``Verifier(python_interpreter=...)``.
    """
    with pytest.raises(ImportError):
        importlib.import_module("codeprobe_zqmr_fakemod")
    site_packages = _staged_site_packages(staged_venv)
    module_path = site_packages / "codeprobe_zqmr_fakemod.py"
    module_path.write_text(
        "ANSWER = 42\n\n\nclass Widget:\n    x: int\n    y: str\n"
        "TUPLE_CONST = (1, 2)\n"
    )
    try:
        yield _staged_python(staged_venv)
    finally:
        module_path.unlink(missing_ok=True)


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
    v = Verifier(manifest, python_interpreter=staged_fake_module)
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
    v = Verifier(manifest, python_interpreter=staged_fake_module)
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
    v = Verifier(manifest, python_interpreter=staged_fake_module)
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


def test_import_equals_subprocess_mode_ignores_pythonpath_shadow(
    tmp_path: Path,
    staged_fake_module: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PYTHONPATH entry that shadows the staged venv's real module must
    NOT be picked up (codeprobe-zqmr finding 1). Without ``-I``, the child
    process would resolve ``codeprobe_zqmr_fakemod`` from the shadow
    directory (ANSWER == 999) instead of the staged venv's site-packages
    (ANSWER == 42), silently smoke-testing the wrong install.
    """
    shadow_dir = tmp_path / "shadow_pkgs"
    shadow_dir.mkdir()
    (shadow_dir / "codeprobe_zqmr_fakemod.py").write_text("ANSWER = 999\n")
    monkeypatch.setenv("PYTHONPATH", str(shadow_dir))

    manifest = tmp_path / "criteria.toml"
    manifest.write_text(textwrap.dedent("""
            [[criterion]]
            id = "IMP-STAGED-NO-SHADOW"
            description = "staged venv's real module wins over a PYTHONPATH shadow"
            tier = "structural"
            check_type = "import_equals"
            severity = "high"
            prd_source = "fake.md#x"
            [criterion.params]
            module = "codeprobe_zqmr_fakemod"
            symbol = "ANSWER"
            expected = 42
            """).strip())
    v = Verifier(manifest, python_interpreter=staged_fake_module)
    verdict = v.run(tmp_path / "ws")
    assert verdict["pass_count"] == 1, verdict["failures"]
    assert verdict["fail_count"] == 0


def test_import_equals_subprocess_mode_ignores_cwd_shadow(
    tmp_path: Path,
    staged_fake_module: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-named module sitting in the caller's cwd must not shadow the
    staged venv's install either (``-c`` prepends cwd to ``sys.path`` unless
    ``-I`` is also passed).
    """
    (tmp_path / "codeprobe_zqmr_fakemod.py").write_text("ANSWER = 111\n")
    monkeypatch.chdir(tmp_path)

    manifest = tmp_path / "criteria.toml"
    manifest.write_text(textwrap.dedent("""
            [[criterion]]
            id = "IMP-STAGED-NO-CWD-SHADOW"
            description = "staged venv's real module wins over a cwd shadow"
            tier = "structural"
            check_type = "import_equals"
            severity = "high"
            prd_source = "fake.md#x"
            [criterion.params]
            module = "codeprobe_zqmr_fakemod"
            symbol = "ANSWER"
            expected = 42
            """).strip())
    v = Verifier(manifest, python_interpreter=staged_fake_module)
    verdict = v.run(tmp_path / "ws")
    assert verdict["pass_count"] == 1, verdict["failures"]
    assert verdict["fail_count"] == 0


# ---------------------------------------------------------------------------
# import_equals tuple/list and dict-key canonicalization (codeprobe-zqmr
# findings 2/3): in-process and subprocess mode must agree on tuple/list and
# dict-key comparisons, not diverge based on whether the value crosses a
# JSON round-trip.
# ---------------------------------------------------------------------------


def test_canonicalize_for_import_equals_tuple_becomes_list() -> None:
    assert _canonicalize_for_import_equals((0.1, 0.9)) == [0.1, 0.9]


def test_canonicalize_for_import_equals_nested_tuple_becomes_nested_list() -> None:
    assert _canonicalize_for_import_equals(((1, 2), (3, 4))) == [[1, 2], [3, 4]]


def test_canonicalize_for_import_equals_dict_int_keys_become_str_keys() -> None:
    assert _canonicalize_for_import_equals({1: "a", 2: "b"}) == {"1": "a", "2": "b"}


def test_import_equals_in_process_tuple_matches_toml_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tuple-valued module constant must compare equal to the TOML array
    it is checked against in-process, matching subprocess-mode's JSON
    round-trip semantics rather than failing on ``tuple != list``.
    """
    (tmp_path / "codeprobe_zqmr_inprocess_tuple_mod.py").write_text(
        "TUPLE_CONST = (1, 2)\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    manifest = tmp_path / "criteria.toml"
    manifest.write_text(textwrap.dedent("""
            [[criterion]]
            id = "IMP-TUPLE-INPROCESS"
            description = "tuple constant equals TOML list, in-process"
            tier = "structural"
            check_type = "import_equals"
            severity = "high"
            prd_source = "fake.md#x"
            [criterion.params]
            module = "codeprobe_zqmr_inprocess_tuple_mod"
            symbol = "TUPLE_CONST"
            expected = [1, 2]
            """).strip())
    v = Verifier(manifest)
    verdict = v.run(tmp_path / "ws")
    assert verdict["pass_count"] == 1, verdict["failures"]
    assert verdict["fail_count"] == 0


def test_import_equals_subprocess_mode_tuple_matches_toml_list(
    tmp_path: Path, staged_fake_module: Path
) -> None:
    """Same tuple-vs-list comparison, this time through subprocess mode —
    proves the two modes agree rather than merely both happening to pass.
    """
    manifest = tmp_path / "criteria.toml"
    manifest.write_text(textwrap.dedent("""
            [[criterion]]
            id = "IMP-TUPLE-STAGED"
            description = "tuple constant equals TOML list, staged interpreter"
            tier = "structural"
            check_type = "import_equals"
            severity = "high"
            prd_source = "fake.md#x"
            [criterion.params]
            module = "codeprobe_zqmr_fakemod"
            symbol = "TUPLE_CONST"
            expected = [1, 2]
            """).strip())
    v = Verifier(manifest, python_interpreter=staged_fake_module)
    verdict = v.run(tmp_path / "ws")
    assert verdict["pass_count"] == 1, verdict["failures"]
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


def test_no_handler_skip_reported_distinctly_from_eval_mode_skip(
    tmp_path: Path,
) -> None:
    """A handler-less check_type is structurally unevaluable in EVERY eval
    mode, unlike an eval_mode-gated criterion which is only unevaluable in
    the current mode. The verdict must report them as distinct counts (and
    list the handler-less criteria with their severity) so a critical
    criterion that can never be checked cannot hide inside a generic
    "mode-skips" bucket."""
    manifest = tmp_path / "criteria.toml"
    manifest.write_text(textwrap.dedent("""
            [[criterion]]
            id = "NO-HANDLER-CRIT"
            description = "check_type with no registered Verifier handler"
            tier = "behavioral"
            check_type = "log_level_matches"
            severity = "critical"
            prd_source = "fake.md#x"
            [criterion.params]

            [[criterion]]
            id = "MODE-GATED"
            description = "only meaningful in full mode"
            tier = "statistical"
            check_type = "count_ge"
            severity = "medium"
            prd_source = "fake.md#x"
            eval_mode_required = "full"
            [criterion.params]
            source = "does-not-matter"
            pattern = "*"
            min_count = 1
            """).strip())
    v = Verifier(manifest, eval_mode=None)
    verdict = v.run(tmp_path / "ws")

    assert verdict["no_handler_count"] == 1
    assert verdict["mode_skip_count"] == 1
    assert verdict["no_handler_criteria"] == [
        {"criterion_id": "NO-HANDLER-CRIT", "tier": "behavioral", "severity": "critical"}
    ]


# ---------------------------------------------------------------------------
# dataclass_roundtrip (structural)
# ---------------------------------------------------------------------------


def _roundtrip_manifest(tmp_path: Path, fixture_rel: str) -> Path:
    manifest = tmp_path / "criteria.toml"
    manifest.write_text(
        textwrap.dedent(f"""
            [[criterion]]
            id = "RT-1"
            description = "results round-trip through CompletedTask"
            tier = "structural"
            check_type = "dataclass_roundtrip"
            severity = "critical"
            prd_source = "fake.md#R16"
            [criterion.params]
            module = "codeprobe.models.experiment"
            symbol = "CompletedTask"
            fixture = "{fixture_rel}"
            """).strip()
    )
    return manifest


def test_dataclass_roundtrip_passes_on_faithful_fixture(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "acceptance").mkdir(parents=True)
    (project / "fx").mkdir()
    (project / "fx" / "results.json").write_text(
        json.dumps(
            {
                "completed": [
                    {"task_id": "t1", "automated_score": 1.0, "cost_source": "cli"},
                    {"task_id": "t2", "automated_score": 0.0, "extra_new_key": "ignored"},
                ]
            }
        )
    )
    manifest = _roundtrip_manifest(project / "acceptance", "fx/results.json")
    v = Verifier(manifest, project_root=project)
    verdict = v.run(tmp_path / "ws")
    assert verdict["pass_count"] == 1
    assert verdict["fail_count"] == 0


def test_dataclass_roundtrip_fails_when_required_field_absent(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "acceptance").mkdir(parents=True)
    (project / "fx").mkdir()
    # No task_id / automated_score → CompletedTask cannot be constructed.
    (project / "fx" / "results.json").write_text(
        json.dumps({"completed_tasks": [{"status": "completed"}]})
    )
    manifest = _roundtrip_manifest(project / "acceptance", "fx/results.json")
    v = Verifier(manifest, project_root=project)
    verdict = v.run(tmp_path / "ws")
    assert verdict["fail_count"] == 1
    assert "cannot construct" in verdict["failures"][0]["evidence"]


def test_dataclass_roundtrip_skips_when_fixture_missing(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "acceptance").mkdir(parents=True)
    manifest = _roundtrip_manifest(project / "acceptance", "fx/does-not-exist.json")
    v = Verifier(manifest, project_root=project)
    result = next(
        r for r in _criterion_results(v, tmp_path / "ws") if r["criterion_id"] == "RT-1"
    )
    assert result["result"] == RESULT_SKIP
    assert "fixture not found" in result["evidence"]


def test_dataclass_roundtrip_against_real_fixture(tmp_path: Path) -> None:
    """The committed tests/fixtures/results.json round-trips through the real
    CompletedTask — the exact contract OUT-ROUNDTRIP-002 encodes."""
    project = Path(__file__).resolve().parent.parent
    manifest = _roundtrip_manifest(tmp_path, "tests/fixtures/results.json")
    v = Verifier(manifest, project_root=project)
    verdict = v.run(tmp_path / "ws")
    assert verdict["pass_count"] == 1
    assert verdict["fail_count"] == 0


# ---------------------------------------------------------------------------
# yaml_field_equal (structural)
# ---------------------------------------------------------------------------


def _yaml_equal_manifest(tmp_path: Path) -> Path:
    manifest = tmp_path / "criteria.toml"
    manifest.write_text(
        textwrap.dedent("""
            [[criterion]]
            id = "YAML-EQ"
            description = "both workflows use the same runner image"
            tier = "structural"
            check_type = "yaml_field_equal"
            severity = "medium"
            prd_source = "fake.md#ci"
            [criterion.params]
            files = ["wf/a.yml", "wf/b.yml"]
            jsonpath = "$.jobs.*.runs-on"
            must_match = true
            """).strip()
    )
    return manifest


def test_yaml_field_equal_passes_when_all_equal(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "acceptance").mkdir(parents=True)
    (project / "wf").mkdir()
    (project / "wf" / "a.yml").write_text(
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n  test:\n    runs-on: ubuntu-latest\n"
    )
    (project / "wf" / "b.yml").write_text(
        "jobs:\n  publish:\n    runs-on: ubuntu-latest\n"
    )
    manifest = _yaml_equal_manifest(project / "acceptance")
    v = Verifier(manifest, project_root=project)
    verdict = v.run(tmp_path / "ws")
    assert verdict["pass_count"] == 1


def test_yaml_field_equal_fails_when_images_differ(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "acceptance").mkdir(parents=True)
    (project / "wf").mkdir()
    (project / "wf" / "a.yml").write_text("jobs:\n  build:\n    runs-on: ubuntu-latest\n")
    (project / "wf" / "b.yml").write_text("jobs:\n  publish:\n    runs-on: ubuntu-22.04\n")
    manifest = _yaml_equal_manifest(project / "acceptance")
    v = Verifier(manifest, project_root=project)
    verdict = v.run(tmp_path / "ws")
    assert verdict["fail_count"] == 1
    assert "differ" in verdict["failures"][0]["evidence"]


def test_yaml_field_equal_skips_when_file_missing(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "acceptance").mkdir(parents=True)
    (project / "wf").mkdir()
    (project / "wf" / "a.yml").write_text("jobs:\n  build:\n    runs-on: ubuntu-latest\n")
    # wf/b.yml absent.
    manifest = _yaml_equal_manifest(project / "acceptance")
    v = Verifier(manifest, project_root=project)
    result = next(
        r for r in _criterion_results(v, tmp_path / "ws") if r["criterion_id"] == "YAML-EQ"
    )
    assert result["result"] == RESULT_SKIP


# ---------------------------------------------------------------------------
# stream_separation (behavioral)
# ---------------------------------------------------------------------------


def _stream_manifest(tmp_path: Path, params_toml: str) -> Path:
    manifest = tmp_path / "criteria.toml"
    manifest.write_text(
        textwrap.dedent(f"""
            [[criterion]]
            id = "STREAM"
            description = "stdout/stderr stay separated"
            tier = "behavioral"
            check_type = "stream_separation"
            severity = "critical"
            prd_source = "fake.md#stderr"
            [criterion.params]
            {params_toml}
            """).strip()
    )
    return manifest


def test_stream_separation_json_stdout_pass_and_fail(tmp_path: Path) -> None:
    manifest = _stream_manifest(
        tmp_path, 'command = "x"\nstdout_must_parse_as = "json"\nwarning_channel = "stderr"'
    )
    v = Verifier(manifest)

    ws_ok = tmp_path / "ok"
    ws_ok.mkdir()
    (ws_ok / "STREAM.stdout").write_text('{"ok": true}\n')
    assert v.run(ws_ok)["pass_count"] == 1

    ws_bad = tmp_path / "bad"
    ws_bad.mkdir()
    (ws_bad / "STREAM.stdout").write_text("WARNING: heads up\n{\"ok\": true}\n")
    assert v.run(ws_bad)["fail_count"] == 1


def test_stream_separation_not_contains_pass_and_fail(tmp_path: Path) -> None:
    manifest = _stream_manifest(
        tmp_path,
        'command = "x"\nstdout_must_not_contain = "INFO codeprobe"\n'
        'stderr_may_contain = "INFO codeprobe"',
    )
    v = Verifier(manifest)

    ws_ok = tmp_path / "ok"
    ws_ok.mkdir()
    (ws_ok / "STREAM.stdout").write_text("pure results, no logs here\n")
    assert v.run(ws_ok)["pass_count"] == 1

    ws_bad = tmp_path / "bad"
    ws_bad.mkdir()
    (ws_bad / "STREAM.stdout").write_text("INFO codeprobe: leaked onto stdout\n")
    assert v.run(ws_bad)["fail_count"] == 1


def test_stream_separation_skips_when_stdout_missing(tmp_path: Path) -> None:
    manifest = _stream_manifest(tmp_path, 'command = "x"\nstdout_must_parse_as = "json"')
    v = Verifier(manifest)
    ws = tmp_path / "ws"
    ws.mkdir()
    result = next(
        r for r in _criterion_results(v, ws) if r["criterion_id"] == "STREAM"
    )
    assert result["result"] == RESULT_SKIP
    assert "missing" in result["evidence"]


# ---------------------------------------------------------------------------
# json_lines_valid (behavioral)
# ---------------------------------------------------------------------------


def _json_lines_manifest(tmp_path: Path) -> Path:
    manifest = tmp_path / "criteria.toml"
    manifest.write_text(
        textwrap.dedent("""
            [[criterion]]
            id = "JLINES"
            description = "one valid JSON object per log line"
            tier = "behavioral"
            check_type = "json_lines_valid"
            severity = "high"
            prd_source = "fake.md#json-events"
            [criterion.params]
            command = "x"
            channel = "stderr"
            required_keys = ["level", "logger", "message"]
            """).strip()
    )
    return manifest


def test_json_lines_valid_pass(tmp_path: Path) -> None:
    v = Verifier(_json_lines_manifest(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "JLINES.stderr").write_text(
        '{"level": "INFO", "logger": "codeprobe", "message": "started"}\n'
        '{"level": "DEBUG", "logger": "codeprobe.mine", "message": "3 tasks"}\n'
    )
    assert v.run(ws)["pass_count"] == 1


def test_json_lines_valid_fails_on_non_json_line(tmp_path: Path) -> None:
    v = Verifier(_json_lines_manifest(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "JLINES.stderr").write_text(
        '{"level": "INFO", "logger": "codeprobe", "message": "ok"}\n'
        "Traceback (most recent call last):\n"
    )
    verdict = v.run(ws)
    assert verdict["fail_count"] == 1
    assert "not valid JSON" in verdict["failures"][0]["evidence"]


def test_json_lines_valid_fails_on_missing_required_key(tmp_path: Path) -> None:
    v = Verifier(_json_lines_manifest(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "JLINES.stderr").write_text('{"level": "INFO", "message": "no logger key"}\n')
    verdict = v.run(ws)
    assert verdict["fail_count"] == 1
    assert "missing required keys" in verdict["failures"][0]["evidence"]


def test_json_lines_valid_skips_when_channel_artifact_missing(tmp_path: Path) -> None:
    v = Verifier(_json_lines_manifest(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    result = next(r for r in _criterion_results(v, ws) if r["criterion_id"] == "JLINES")
    assert result["result"] == RESULT_SKIP
    assert "missing" in result["evidence"]


def test_json_lines_valid_fails_when_channel_empty(tmp_path: Path) -> None:
    v = Verifier(_json_lines_manifest(tmp_path))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "JLINES.stderr").write_text("\n  \n")
    verdict = v.run(ws)
    assert verdict["fail_count"] == 1
    assert "no non-empty lines" in verdict["failures"][0]["evidence"]


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
