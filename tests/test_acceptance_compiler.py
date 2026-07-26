"""Unit tests for acceptance_compiler — criterion-driven Test Agent action compiler.

TDD RED phase: all tests written against the planned API before implementation.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from acceptance.loader import Criterion, load_criteria
from codeprobe.acceptance_compiler import TestAction, compile_actions

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TARGET_REPO = Path("/fake/target/repo")
WORKSPACE = Path("/fake/workspace")
PROJECT_ROOT = Path("/fake/project")


def _criterion(
    id: str = "TEST-001",
    check_type: str = "cli_exit_code",
    tier: str = "behavioral",
    params: dict | None = None,
) -> Criterion:
    return Criterion(
        id=id,
        description="test criterion",
        tier=tier,
        check_type=check_type,
        severity="high",
        prd_source="docs/prd/test.md",
        depends_on=(),
        params=params or {},
    )


# ---------------------------------------------------------------------------
# TestAction dataclass
# ---------------------------------------------------------------------------


class TestTestAction:
    def test_frozen(self) -> None:
        action = TestAction(
            criterion_id="X",
            description="d",
            shell_snippet="echo hi",
            artifact_paths=("x.exit",),
        )
        with pytest.raises(AttributeError):
            action.criterion_id = "Y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Structural check_types produce NO actions
# ---------------------------------------------------------------------------


class TestStructuralSkip:
    @pytest.mark.parametrize(
        "check_type",
        [
            "import_equals",
            "dataclass_has_fields",
            "dataclass_roundtrip",
            "regex_present",
            "regex_absent",
            "pyproject_deps_bounded",
            "yaml_field_equal",
        ],
    )
    def test_structural_types_produce_no_action(self, check_type: str) -> None:
        c = _criterion(check_type=check_type)
        actions = compile_actions(
            [c],
            target_repo=TARGET_REPO,
            workspace=WORKSPACE,
            project_root=PROJECT_ROOT,
        )
        assert actions == []


# ---------------------------------------------------------------------------
# Handler-less check_types produce NO actions
# ---------------------------------------------------------------------------


class TestHandlerlessSkip:
    @pytest.mark.parametrize(
        "check_type",
        [
            # log_level_matches is the only remaining handler-less type: a
            # subprocess's logger level is not observable from --help, so no
            # honest handler exists and no artifact is worth emitting
            # (codeprobe-2s54).
            "log_level_matches",
        ],
    )
    def test_handlerless_types_produce_no_action(self, check_type: str) -> None:
        c = _criterion(check_type=check_type)
        actions = compile_actions(
            [c],
            target_repo=TARGET_REPO,
            workspace=WORKSPACE,
            project_root=PROJECT_ROOT,
        )
        assert actions == []


# ---------------------------------------------------------------------------
# Behavioral stream-capture check_types now emit a command-capture action
# ---------------------------------------------------------------------------


class TestStreamCaptureEmitters:
    @pytest.mark.parametrize("check_type", ["stream_separation", "json_lines_valid"])
    def test_captures_stdout_stderr_exit(self, check_type: str) -> None:
        c = _criterion(
            id="STREAM-001",
            check_type=check_type,
            params={"command": "codeprobe interpret {results} --format json"},
        )
        actions = compile_actions(
            [c],
            target_repo=TARGET_REPO,
            workspace=WORKSPACE,
            project_root=PROJECT_ROOT,
        )
        assert len(actions) == 1
        a = actions[0]
        assert "STREAM-001.stdout" in a.artifact_paths
        assert "STREAM-001.stderr" in a.artifact_paths
        assert "STREAM-001.exit" in a.artifact_paths
        # {results} token was substituted, not left literal.
        assert "{results}" not in a.shell_snippet

    def test_workspace_token_substituted(self) -> None:
        """``{workspace}`` resolves to the workspace path.

        Regression for the LOG-STDERR-003 / OUT-JSON-LINES-001 fix: probe
        output is routed under the isolated workspace (``--output
        {workspace}/...``) instead of dirtying the target repo across
        iterations, so the token must substitute like ``{repo}`` does.
        """
        c = _criterion(
            id="WS-TOKEN-001",
            check_type="stream_separation",
            params={"command": "codeprobe probe {repo} --output {workspace}/probes"},
        )
        actions = compile_actions(
            [c],
            target_repo=TARGET_REPO,
            workspace=WORKSPACE,
            project_root=PROJECT_ROOT,
        )
        assert len(actions) == 1
        snippet = actions[0].shell_snippet
        assert "{workspace}" not in snippet
        assert f"{WORKSPACE}/probes" in snippet

    @pytest.mark.parametrize("check_type", ["stream_separation", "json_lines_valid"])
    def test_missing_command_returns_stub(self, check_type: str) -> None:
        c = _criterion(id="STREAM-STUB", check_type=check_type, params={})
        actions = compile_actions(
            [c],
            target_repo=TARGET_REPO,
            workspace=WORKSPACE,
            project_root=PROJECT_ROOT,
        )
        assert len(actions) == 1
        assert "COMPILE_ERROR" in actions[0].shell_snippet


# ---------------------------------------------------------------------------
# Unknown check_type produces NO action
# ---------------------------------------------------------------------------


class TestUnknownType:
    def test_totally_unknown_type_produces_no_action(self) -> None:
        c = _criterion(check_type="quantum_teleport")
        actions = compile_actions(
            [c],
            target_repo=TARGET_REPO,
            workspace=WORKSPACE,
            project_root=PROJECT_ROOT,
        )
        assert actions == []


# ---------------------------------------------------------------------------
# cli_exit_code
# ---------------------------------------------------------------------------


class TestCliExitCode:
    def test_basic_command(self) -> None:
        c = _criterion(
            id="BUG-001",
            check_type="cli_exit_code",
            params={"command": "codeprobe mine {repo}", "expected_exit": 0},
        )
        actions = compile_actions(
            [c],
            target_repo=TARGET_REPO,
            workspace=WORKSPACE,
            project_root=PROJECT_ROOT,
        )
        assert len(actions) == 1
        a = actions[0]
        assert a.criterion_id == "BUG-001"
        assert str(TARGET_REPO) in a.shell_snippet
        assert "BUG-001.exit" in a.shell_snippet
        assert "BUG-001.stdout" in a.shell_snippet
        assert "BUG-001.stderr" in a.shell_snippet
        assert "BUG-001.exit" in a.artifact_paths
        assert "BUG-001.stdout" in a.artifact_paths
        assert "BUG-001.stderr" in a.artifact_paths

    def test_missing_command_returns_stub(self) -> None:
        c = _criterion(
            id="BUG-STUB",
            check_type="cli_exit_code",
            params={"expected_exit": 0},
        )
        actions = compile_actions(
            [c],
            target_repo=TARGET_REPO,
            workspace=WORKSPACE,
            project_root=PROJECT_ROOT,
        )
        assert len(actions) == 1
        a = actions[0]
        assert "COMPILE_ERROR" in a.shell_snippet
        assert "BUG-STUB.exit" in a.artifact_paths


# ---------------------------------------------------------------------------
# cli_help_contains
# ---------------------------------------------------------------------------


class TestCliHelpContains:
    def test_multiple_commands(self) -> None:
        c = _criterion(
            id="HELP-001",
            check_type="cli_help_contains",
            params={
                "commands": ["codeprobe mine --help", "codeprobe run --help"],
                "must_contain": "--out",
            },
        )
        actions = compile_actions(
            [c],
            target_repo=TARGET_REPO,
            workspace=WORKSPACE,
            project_root=PROJECT_ROOT,
        )
        assert len(actions) == 1
        a = actions[0]
        assert "mine --help" in a.shell_snippet
        assert "run --help" in a.shell_snippet
        # All outputs appended to same stdout file
        assert "HELP-001.stdout" in a.shell_snippet
        assert "HELP-001.stdout" in a.artifact_paths


# ---------------------------------------------------------------------------
# cli_stdout_contains / stdout_contains
# ---------------------------------------------------------------------------


class TestCliStdoutContains:
    def test_produces_stdout_artifact(self) -> None:
        c = _criterion(
            id="STDOUT-001",
            check_type="cli_stdout_contains",
            params={
                "command": "codeprobe validate {tasks_dir}",
                "must_contain": "task-001",
                "fixture": "tests/fixtures/nested_tasks",
            },
        )
        actions = compile_actions(
            [c],
            target_repo=TARGET_REPO,
            workspace=WORKSPACE,
            project_root=PROJECT_ROOT,
        )
        assert len(actions) == 1
        a = actions[0]
        assert "STDOUT-001.stdout" in a.artifact_paths
        # {tasks_dir} should be resolved using project_root + fixture
        assert (
            "tests/fixtures/nested_tasks" in a.shell_snippet
            or str(PROJECT_ROOT) in a.shell_snippet
        )

    def test_stdout_contains_alias(self) -> None:
        c = _criterion(
            id="SC-001",
            check_type="stdout_contains",
            params={"command": "echo hello", "must_contain": "hello"},
        )
        actions = compile_actions(
            [c],
            target_repo=TARGET_REPO,
            workspace=WORKSPACE,
            project_root=PROJECT_ROOT,
        )
        assert len(actions) == 1
        assert "SC-001.stdout" in actions[0].artifact_paths


# ---------------------------------------------------------------------------
# stderr_contains
# ---------------------------------------------------------------------------


class TestStderrContains:
    def test_produces_stderr_artifact(self) -> None:
        c = _criterion(
            id="STDERR-001",
            check_type="stderr_contains",
            params={"command": "codeprobe run", "must_contain": "WARNING"},
        )
        actions = compile_actions(
            [c],
            target_repo=TARGET_REPO,
            workspace=WORKSPACE,
            project_root=PROJECT_ROOT,
        )
        assert len(actions) == 1
        a = actions[0]
        assert "STDERR-001.stderr" in a.artifact_paths
        assert "STDERR-001.stdout" in a.artifact_paths


# ---------------------------------------------------------------------------
# cli_writes_file
# ---------------------------------------------------------------------------


class TestCliWritesFile:
    def test_expected_path(self) -> None:
        c = _criterion(
            id="WRITE-001",
            check_type="cli_writes_file",
            params={
                "command": "codeprobe experiment init --non-interactive",
                "expected_path": ".codeprobe/experiment.json",
            },
        )
        actions = compile_actions(
            [c],
            target_repo=TARGET_REPO,
            workspace=WORKSPACE,
            project_root=PROJECT_ROOT,
        )
        assert len(actions) == 1
        a = actions[0]
        # Command runs from workspace dir
        assert str(WORKSPACE) in a.shell_snippet
        assert ".codeprobe/experiment.json" in a.artifact_paths


# ---------------------------------------------------------------------------
# file_exists
# ---------------------------------------------------------------------------


class TestFileExists:
    def test_path_param(self) -> None:
        c = _criterion(
            id="FE-001",
            check_type="file_exists",
            params={"path": "results/output.json"},
        )
        actions = compile_actions(
            [c],
            target_repo=TARGET_REPO,
            workspace=WORKSPACE,
            project_root=PROJECT_ROOT,
        )
        assert len(actions) == 1
        assert "results/output.json" in actions[0].artifact_paths


# ---------------------------------------------------------------------------
# count_ge
# ---------------------------------------------------------------------------


class TestCountGe:
    def test_sync_action(self) -> None:
        c = _criterion(
            id="COUNT-001",
            check_type="count_ge",
            tier="statistical",
            params={
                "source": "{repo}/.codeprobe/tasks",
                "pattern": "task-*",
                "min_count": 3,
            },
        )
        actions = compile_actions(
            [c],
            target_repo=TARGET_REPO,
            workspace=WORKSPACE,
            project_root=PROJECT_ROOT,
        )
        assert len(actions) == 1
        a = actions[0]
        # Should sync target_repo/.codeprobe into workspace
        assert str(TARGET_REPO) in a.shell_snippet
        assert ".codeprobe" in a.shell_snippet


# ---------------------------------------------------------------------------
# json_count_ge
# ---------------------------------------------------------------------------


class TestJsonCountGe:
    def test_sync_action(self) -> None:
        c = _criterion(
            id="JCOUNT-001",
            check_type="json_count_ge",
            tier="statistical",
            params={
                "source": "{repo}/.codeprobe/results.json",
                "jsonpath": "$.completed_tasks",
                "min_count": 1,
            },
        )
        actions = compile_actions(
            [c],
            target_repo=TARGET_REPO,
            workspace=WORKSPACE,
            project_root=PROJECT_ROOT,
        )
        assert len(actions) == 1
        a = actions[0]
        assert str(TARGET_REPO) in a.shell_snippet


# ---------------------------------------------------------------------------
# json_field_not_null / json_field_equals / json_field_type
# ---------------------------------------------------------------------------


class TestJsonFieldChecks:
    @pytest.mark.parametrize(
        "check_type",
        ["json_field_not_null", "json_field_equals", "json_field_type"],
    )
    def test_json_field_produces_sync_action(self, check_type: str) -> None:
        c = _criterion(
            id="JF-001",
            check_type=check_type,
            tier="statistical",
            params={"source": "{repo}/.codeprobe/results.json", "jsonpath": "$.x"},
        )
        actions = compile_actions(
            [c],
            target_repo=TARGET_REPO,
            workspace=WORKSPACE,
            project_root=PROJECT_ROOT,
        )
        assert len(actions) == 1
        assert str(TARGET_REPO) in actions[0].shell_snippet


# ---------------------------------------------------------------------------
# canary_detect
# ---------------------------------------------------------------------------


class TestCanaryDetect:
    def test_writes_canary_txt(self) -> None:
        c = _criterion(
            id="CANARY-001",
            check_type="canary_detect",
            tier="statistical",
            params={
                "canary_env": "CODEPROBE_CANARY_UUID",
                "search_in": "{repo}/.codeprobe/results.json",
            },
        )
        actions = compile_actions(
            [c],
            target_repo=TARGET_REPO,
            workspace=WORKSPACE,
            project_root=PROJECT_ROOT,
        )
        assert len(actions) == 1
        a = actions[0]
        assert "canary.txt" in a.shell_snippet
        assert "CODEPROBE_CANARY_UUID" in a.shell_snippet
        assert "canary.txt" in a.artifact_paths


# ---------------------------------------------------------------------------
# Mixed criteria
# ---------------------------------------------------------------------------


class TestMixedCriteria:
    def test_mixed_list_filters_structural(self) -> None:
        criteria = [
            _criterion(
                id="A",
                check_type="cli_exit_code",
                params={"command": "echo hi", "expected_exit": 0},
            ),
            _criterion(
                id="B",
                check_type="regex_present",
                tier="structural",
                params={"file": "x.py", "pattern": "foo"},
            ),
            _criterion(
                id="C",
                check_type="import_equals",
                tier="structural",
                params={"module": "x", "symbol": "Y", "expected": 1},
            ),
            _criterion(
                id="D",
                check_type="cli_stdout_contains",
                params={"command": "echo test", "must_contain": "test"},
            ),
        ]
        actions = compile_actions(
            criteria,
            target_repo=TARGET_REPO,
            workspace=WORKSPACE,
            project_root=PROJECT_ROOT,
        )
        ids = [a.criterion_id for a in actions]
        assert "A" in ids
        assert "D" in ids
        assert "B" not in ids
        assert "C" not in ids


# ---------------------------------------------------------------------------
# Real criteria.toml
# ---------------------------------------------------------------------------


class TestRealCriteria:
    def test_load_and_compile_no_exceptions(self) -> None:
        criteria = load_criteria()
        actions = compile_actions(
            criteria,
            target_repo=TARGET_REPO,
            workspace=WORKSPACE,
            project_root=Path(__file__).resolve().parent.parent,
        )
        # At least 9 actions (behavioral + statistical that have handlers)
        assert len(actions) >= 9
        # Every action has non-empty fields
        for a in actions:
            assert a.criterion_id
            assert a.shell_snippet
            assert a.artifact_paths

    def test_no_duplicate_criterion_ids_in_actions(self) -> None:
        criteria = load_criteria()
        actions = compile_actions(
            criteria,
            target_repo=TARGET_REPO,
            workspace=WORKSPACE,
            project_root=Path(__file__).resolve().parent.parent,
        )
        ids = [a.criterion_id for a in actions]
        assert len(ids) == len(set(ids))

    # -- The four approved manifest corrections (codeprobe-nien). Guard the
    #    real acceptance/criteria.toml so a future edit cannot silently revert
    #    a fix back into its deterministic-failure shape.

    def _by_id(self, cid: str) -> Criterion:
        for c in load_criteria():
            if c.id == cid:
                return c
        raise AssertionError(f"criterion {cid} missing from manifest")

    def test_interpret_criterion_uses_repo_token(self) -> None:
        c = self._by_id("BUG-INTERPRET-STDOUT-003")
        cmd = c.params["command"]
        assert "{repo}" in cmd
        assert "{results}" not in cmd

    def test_mine_count_pattern_counts_instruction_md_min_3(self) -> None:
        c = self._by_id("SILENT-MINE-COUNT-001")
        assert c.params["pattern"] == "*/instruction.md"
        # min_count is unchanged: the fix is the pattern, not the threshold.
        assert c.params["min_count"] == 3
        assert c.severity == "critical"

    def test_log_stderr_probe_output_rooted_in_workspace(self) -> None:
        c = self._by_id("LOG-STDERR-003")
        cmd = c.params["command"]
        assert cmd.startswith("codeprobe probe {repo}")
        assert "--output {workspace}/" in cmd
        # A pinned, deterministic probe count keeps the check reproducible.
        assert "--count 5" in cmd

    def test_out_json_lines_is_deterministic_probe_under_workspace(self) -> None:
        c = self._by_id("OUT-JSON-LINES-001")
        cmd = c.params["command"]
        assert "--log-format json" in cmd
        # Deterministic, zero-budget: a probe, never an agent 'run'.
        assert " probe " in f" {cmd} "
        assert " run " not in f" {cmd} "
        assert "--output {workspace}/" in cmd
        assert "--count 5" in cmd

    def test_validate_discovery_action_exits_zero_and_finds_task_001(
        self, tmp_path: Path
    ) -> None:
        """Execute the REAL compiled BUG-VALIDATE-DISCOVERY-005 action end to
        end and assert BOTH the captured exit code is 0 AND stdout contains
        ``task-001``.

        The criterion's ``cli_stdout_contains`` check inspects only stdout, so
        a nonzero exit (an invalid fixture task) with ``task-001`` still in the
        listing would green vacuously. Asserting ``.exit == 0`` here closes
        that gap: the acceptance fixture must validate cleanly for the rehearsal
        green to be real.
        """
        # Resolve ``codeprobe`` from the directory of the running interpreter
        # (the venv/editable install under test), not from ambient PATH — a
        # stale ``codeprobe`` earlier on PATH would exercise the wrong code and
        # give a misleading result. Prepending that dir makes the run
        # deterministic regardless of how PATH is set outside the test.
        bindir = Path(sys.executable).parent
        assert (bindir / "codeprobe").exists(), (
            f"codeprobe not installed next to interpreter: {bindir / 'codeprobe'}"
        )
        env = {
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        }
        repo_root = Path(__file__).resolve().parent.parent
        crit = next(
            c for c in load_criteria() if c.id == "BUG-VALIDATE-DISCOVERY-005"
        )
        workspace = tmp_path / "ws"
        workspace.mkdir()
        actions = compile_actions(
            [crit],
            target_repo=repo_root,
            workspace=workspace,
            project_root=repo_root,
        )
        assert len(actions) == 1
        subprocess.run(
            ["bash", "-c", actions[0].shell_snippet],
            cwd=workspace,
            env=env,
            check=False,
        )
        exit_code = (workspace / f"{crit.id}.exit").read_text().strip()
        stdout = (workspace / f"{crit.id}.stdout").read_text()
        assert exit_code == "0", f"validate exited {exit_code}; stdout={stdout!r}"
        assert "task-001" in stdout, stdout

    def test_workspace_rooted_criteria_compile_output_into_workspace(self) -> None:
        """The probe-based criteria must compile with their output path under
        the workspace, not the target repo — otherwise consecutive iterations
        re-dirty the target."""
        actions = {
            a.criterion_id: a
            for a in compile_actions(
                load_criteria(),
                target_repo=TARGET_REPO,
                workspace=WORKSPACE,
                project_root=Path(__file__).resolve().parent.parent,
            )
        }
        for cid in ("LOG-STDERR-003", "OUT-JSON-LINES-001"):
            snippet = actions[cid].shell_snippet
            assert f"--output {WORKSPACE}/" in snippet, cid
            assert "{workspace}" not in snippet, cid
