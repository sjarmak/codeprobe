"""Tests for ``scripts/acceptance_loop.py``.

Uses stub manifests (in the style of ``tests/test_verifier.py``) so no real
agent runs happen. The orchestrator is pure plumbing — these tests prove
that it compiles actions, executes snippets, persists verdicts into the
history directory, and honors the convergence controller's decisions.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
from pathlib import Path

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "acceptance_loop.py"
)
_SPEC = importlib.util.spec_from_file_location("acceptance_loop_script", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
acceptance_loop = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = acceptance_loop
_SPEC.loader.exec_module(acceptance_loop)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def passing_manifest(tmp_path: Path) -> Path:
    """One structural pass + one behavioral pass + one mode-gated criterion."""
    manifest = tmp_path / "criteria.toml"
    manifest.write_text(
        textwrap.dedent("""
            [[criterion]]
            id = "STRUCT-PASS"
            description = "Criterion dataclass has an id field"
            tier = "structural"
            check_type = "dataclass_has_fields"
            severity = "high"
            prd_source = "fake.md#x"
            [criterion.params]
            module = "acceptance.loader"
            symbol = "Criterion"
            required_fields = ["id"]

            [[criterion]]
            id = "BEHAV-TRUE"
            description = "true exits zero"
            tier = "behavioral"
            check_type = "cli_exit_code"
            severity = "high"
            prd_source = "fake.md#x"
            [criterion.params]
            command = "true"
            expected_exit = 0

            [[criterion]]
            id = "GATED-FULL"
            description = "only meaningful in full mode"
            tier = "statistical"
            check_type = "count_ge"
            severity = "critical"
            prd_source = "fake.md#x"
            eval_mode_required = "full"
            [criterion.params]
            source = "{repo}/.codeprobe/tasks"
            pattern = "task-*"
            min_count = 1
            """).strip()
    )
    return manifest


@pytest.fixture()
def failing_manifest(tmp_path: Path) -> Path:
    """A behavioral criterion that fails identically every iteration."""
    manifest = tmp_path / "criteria.toml"
    manifest.write_text(
        textwrap.dedent("""
            [[criterion]]
            id = "BEHAV-FALSE"
            description = "false exits nonzero but zero is expected"
            tier = "behavioral"
            check_type = "cli_exit_code"
            severity = "high"
            prd_source = "fake.md#x"
            [criterion.params]
            command = "false"
            expected_exit = 0
            """).strip()
    )
    return manifest


def _loop_args(
    tmp_path: Path,
    manifest: Path,
    *,
    iterations: int = 1,
    extra: list[str] | None = None,
) -> list[str]:
    return [
        "--iterations",
        str(iterations),
        "--eval-mode",
        "default",
        "--repo-root",
        str(Path(__file__).resolve().parent.parent),
        "--criteria",
        str(manifest),
        "--history-dir",
        str(tmp_path / "history"),
        "--workspace-root",
        str(tmp_path / "workspaces"),
        *(extra or []),
    ]


def _read_verdicts(tmp_path: Path) -> list[dict]:
    history = tmp_path / "history"
    return [
        json.loads(p.read_text())
        for p in sorted(history.glob("verdict-*.json"))
    ]


# ---------------------------------------------------------------------------
# End-to-end orchestration
# ---------------------------------------------------------------------------


def test_single_iteration_writes_verdict(
    tmp_path: Path, passing_manifest: Path
) -> None:
    exit_code = acceptance_loop.main(_loop_args(tmp_path, passing_manifest))
    assert exit_code == 0

    verdicts = _read_verdicts(tmp_path)
    assert len(verdicts) == 1
    verdict = verdicts[0]
    assert verdict["iteration"] == 1
    assert verdict["status"] == "EVALUATED"
    assert verdict["all_pass"] is True
    assert verdict["pass_count"] == 2  # STRUCT-PASS + BEHAV-TRUE
    assert verdict["fail_count"] == 0
    assert verdict["eval_mode"] is None  # default mode


def test_behavioral_action_executed_in_workspace(
    tmp_path: Path, passing_manifest: Path
) -> None:
    """The compiled snippet must have produced the exit artifact."""
    acceptance_loop.main(_loop_args(tmp_path, passing_manifest))
    workspace = tmp_path / "workspaces" / "iter-0001"
    assert (workspace / "BEHAV-TRUE.exit").read_text().strip() == "0"


def test_mode_gated_criteria_excluded_in_default_mode(
    tmp_path: Path, passing_manifest: Path
) -> None:
    acceptance_loop.main(_loop_args(tmp_path, passing_manifest))
    verdict = _read_verdicts(tmp_path)[0]
    assert verdict["mode_skip_count"] == 1  # GATED-FULL
    # No sync action was executed for the gated statistical criterion.
    workspace = tmp_path / "workspaces" / "iter-0001"
    assert not (workspace / "GATED-FULL.synced").exists()


def test_release_decision_stops_loop_early(
    tmp_path: Path, passing_manifest: Path
) -> None:
    """Two consecutive greens → RELEASE → no third iteration."""
    exit_code = acceptance_loop.main(
        _loop_args(tmp_path, passing_manifest, iterations=3)
    )
    assert exit_code == 0
    assert len(_read_verdicts(tmp_path)) == 2


def test_escalate_on_three_identical_failures(
    tmp_path: Path, failing_manifest: Path
) -> None:
    """Same criterion failing 3x with identical evidence → ESCALATE, exit 1."""
    exit_code = acceptance_loop.main(
        _loop_args(tmp_path, failing_manifest, iterations=5)
    )
    assert exit_code == 1
    # The loop stopped at the escalation, not the iteration cap.
    assert len(_read_verdicts(tmp_path)) == 3


def test_action_timeout_yields_honest_incomplete(tmp_path: Path) -> None:
    """A hung action leaves no artifacts → skip → INCOMPLETE, never pass."""
    manifest = tmp_path / "criteria.toml"
    manifest.write_text(
        textwrap.dedent("""
            [[criterion]]
            id = "BEHAV-HANG"
            description = "sleeps past the action timeout"
            tier = "behavioral"
            check_type = "cli_exit_code"
            severity = "high"
            prd_source = "fake.md#x"
            [criterion.params]
            command = "sleep 5"
            expected_exit = 0
            """).strip()
    )
    exit_code = acceptance_loop.main(
        _loop_args(tmp_path, manifest, extra=["--action-timeout", "0.2"])
    )
    assert exit_code == 0  # CONTINUE is not a failure of the orchestrator
    verdict = _read_verdicts(tmp_path)[0]
    assert verdict["status"] == "INCOMPLETE"
    assert verdict["all_pass"] is False
    assert verdict["skip_count"] == 1


def test_hung_stdout_contains_action_does_not_yield_false_pass(
    tmp_path: Path,
) -> None:
    """Reproduces the silent-pass-through: the command prints the expected
    substring and then hangs. Bash's ``( cmd ) > out 2> err`` redirection
    creates and flushes the ``.stdout`` artifact before the hang, and the
    ``.exit`` file is never written (the loop kills the process at the
    timeout). Only ``cli_exit_code`` reads ``.exit``; ``stdout_contains``
    reads only ``.stdout``, so without poisoning the artifacts on timeout
    this criterion would PASS on a command that never finished.
    """
    manifest = tmp_path / "criteria.toml"
    manifest.write_text(
        textwrap.dedent("""
            [[criterion]]
            id = "MINING-HANG"
            description = "prints then hangs past the action timeout"
            tier = "behavioral"
            check_type = "stdout_contains"
            severity = "critical"
            prd_source = "fake.md#x"
            [criterion.params]
            command = "echo MINING COMPLETE; sleep 5"
            must_contain = "MINING COMPLETE"
            """).strip()
    )
    exit_code = acceptance_loop.main(
        _loop_args(tmp_path, manifest, extra=["--action-timeout", "0.3"])
    )
    assert exit_code == 0  # CONTINUE, not a crash
    verdict = _read_verdicts(tmp_path)[0]
    assert verdict["pass_count"] == 0
    assert verdict["skip_count"] == 1
    assert verdict["all_pass"] is False
    workspace = tmp_path / "workspaces" / "iter-0001"
    assert not (workspace / "MINING-HANG.stdout").exists()


def test_execute_action_poisons_preexisting_artifact_on_timeout(
    tmp_path: Path,
) -> None:
    """Directly exercises ``execute_action``: a partial artifact left by a
    hung command (simulated here) must be removed, not evaluated."""
    workspace = tmp_path
    (workspace / "HANG.stdout").write_text("MINING COMPLETE\n")
    action = acceptance_loop.TestAction(
        criterion_id="HANG",
        description="hang",
        shell_snippet="sleep 5",
        artifact_paths=("HANG.stdout", "HANG.stderr", "HANG.exit"),
    )
    record = acceptance_loop.execute_action(action, workspace, timeout_s=0.2)
    assert record["outcome"] == "timeout"
    assert not (workspace / "HANG.stdout").exists()


def test_execute_action_kills_grandchild_process_group_on_timeout(
    tmp_path: Path,
) -> None:
    """A timeout must kill the WHOLE process group, not just ``bash``. The
    compiler always wraps the real command in a ``( cmd ) > out 2> err``
    subshell, so the real command is a grandchild of ``bash -c``; without
    ``start_new_session`` + ``killpg`` that grandchild survives as an
    orphan and can keep writing into shared state after the loop moves on.
    """
    workspace = tmp_path
    marker = workspace / "orphan-wrote-this"
    action = acceptance_loop.TestAction(
        criterion_id="ORPHAN",
        description="grandchild outlives its parent",
        shell_snippet=f'( sleep 0.5 && touch "{marker}" ) & wait',
        artifact_paths=(),
    )
    record = acceptance_loop.execute_action(action, workspace, timeout_s=0.1)
    assert record["outcome"] == "timeout"
    # Give the (would-be) orphan the time it needed to write the marker.
    import time

    time.sleep(1.0)
    assert not marker.exists()


def test_producer_timeout_does_not_leak_stale_target_repo_state(
    tmp_path: Path,
) -> None:
    """Reproduces the cross-iteration silent-pass-through: a full-mode
    producer (simulating ``codeprobe mine``) writes 3 tasks into the shared,
    persistent ``target_repo/.codeprobe/tasks`` on iteration 1, then hangs
    past the action timeout on iteration 2 (simulating a stall before it
    touches ``.codeprobe`` again). Without resetting ``target_repo/
    .codeprobe`` before each full-mode iteration, the dependent's sync
    action would re-copy iteration 1's stale 3 tasks into iteration 2's
    fresh workspace and the statistical ``count_ge`` check would falsely
    PASS on data iteration 2 never produced, even though the producer
    itself was honestly skipped (poisoned artifacts on timeout).
    """
    manifest = tmp_path / "criteria.toml"
    manifest.write_text(
        textwrap.dedent('''
            [[criterion]]
            id = "PRODUCER"
            description = "simulated codeprobe mine: writes 3 tasks once, then hangs"
            tier = "behavioral"
            check_type = "cli_exit_code"
            severity = "critical"
            prd_source = "fake.md#x"
            eval_mode_required = "full"
            [criterion.params]
            command = """\\
            if [ -f {repo}/.marker ]; then sleep 5; else \\
            mkdir -p {repo}/.codeprobe/tasks && \\
            touch {repo}/.codeprobe/tasks/task-{a,b,c} {repo}/.marker; fi\\
            """
            expected_exit = 0

            [[criterion]]
            id = "DEPENDENT"
            description = "at least 3 tasks produced this iteration"
            tier = "statistical"
            check_type = "count_ge"
            severity = "critical"
            prd_source = "fake.md#x"
            depends_on = ["PRODUCER"]
            eval_mode_required = "full"
            [criterion.params]
            source = "{repo}/.codeprobe/tasks"
            pattern = "task-*"
            min_count = 3
            ''').strip()
    )
    target_repo = tmp_path / "target"
    target_repo.mkdir()

    args = [
        "--iterations", "1",
        "--eval-mode", "full",
        "--repo-root", str(Path(__file__).resolve().parent.parent),
        "--criteria", str(manifest),
        "--history-dir", str(tmp_path / "history"),
        "--workspace-root", str(tmp_path / "workspaces"),
        "--target-repo", str(target_repo),
        "--action-timeout", "0.3",
    ]

    # Iteration 1: producer succeeds, populates target_repo/.codeprobe/tasks.
    exit_code = acceptance_loop.main(args)
    assert exit_code == 0
    verdict_1 = _read_verdicts(tmp_path)[0]
    assert verdict_1["pass_count"] == 2  # PRODUCER + DEPENDENT both pass
    assert verdict_1["all_pass"] is True
    assert (target_repo / ".codeprobe" / "tasks" / "task-a").exists()

    # Iteration 2: producer hangs past the timeout (marker file survives the
    # .codeprobe-scoped reset, so the simulated hang condition still fires).
    exit_code = acceptance_loop.main(args)
    verdicts = _read_verdicts(tmp_path)
    assert len(verdicts) == 2
    verdict_2 = verdicts[1]

    # The producer was honestly skipped (timeout poisoned its artifacts) —
    # it must never register as a pass.
    assert verdict_2["pass_count"] == 0
    assert verdict_2["all_pass"] is False
    # target_repo/.codeprobe was reset before this iteration, so the
    # dependent's sync action found nothing stale to copy and skipped
    # honestly instead of falsely passing on iteration 1's 3 tasks.
    workspace_2 = tmp_path / "workspaces" / "iter-0002"
    assert not (workspace_2 / ".codeprobe" / "tasks").exists()
    dependent_failures = [
        f for f in verdict_2["failures"] if f["criterion_id"] == "DEPENDENT"
    ]
    assert dependent_failures == []  # skipped, not failed, and critically not passed


def test_reset_target_repo_state_removes_codeprobe_dir(tmp_path: Path) -> None:
    target_repo = tmp_path
    codeprobe_dir = target_repo / ".codeprobe" / "tasks"
    codeprobe_dir.mkdir(parents=True)
    (codeprobe_dir / "task-a").write_text("stale")

    acceptance_loop._reset_target_repo_state(target_repo)

    assert not (target_repo / ".codeprobe").exists()


def test_reset_target_repo_state_is_noop_when_absent(tmp_path: Path) -> None:
    """Must not raise when there is nothing to reset."""
    acceptance_loop._reset_target_repo_state(tmp_path)
    assert not (tmp_path / ".codeprobe").exists()


def test_history_appends_across_invocations(
    tmp_path: Path, passing_manifest: Path
) -> None:
    acceptance_loop.main(_loop_args(tmp_path, passing_manifest))
    acceptance_loop.main(_loop_args(tmp_path, passing_manifest))
    verdicts = _read_verdicts(tmp_path)
    assert [v["iteration"] for v in verdicts] == [1, 2]


def test_full_mode_recorded_in_verdict(tmp_path: Path) -> None:
    manifest = tmp_path / "criteria.toml"
    manifest.write_text(
        textwrap.dedent("""
            [[criterion]]
            id = "STRUCT-PASS"
            description = "Criterion dataclass has an id field"
            tier = "structural"
            check_type = "dataclass_has_fields"
            severity = "high"
            prd_source = "fake.md#x"
            [criterion.params]
            module = "acceptance.loader"
            symbol = "Criterion"
            required_fields = ["id"]
            """).strip()
    )
    args = _loop_args(tmp_path, manifest)
    args[args.index("default")] = "full"
    exit_code = acceptance_loop.main(args)
    assert exit_code == 0
    assert _read_verdicts(tmp_path)[0]["eval_mode"] == "full"


# ---------------------------------------------------------------------------
# Unit-level helpers
# ---------------------------------------------------------------------------


def test_next_iteration_empty_and_populated(tmp_path: Path) -> None:
    assert acceptance_loop.next_iteration(tmp_path) == 1
    (tmp_path / "verdict-0007.json").write_text("{}")
    (tmp_path / "verdict-0002.json").write_text("{}")
    (tmp_path / "not-a-verdict.json").write_text("{}")
    assert acceptance_loop.next_iteration(tmp_path) == 8


def test_select_criteria_filters_on_mode(passing_manifest: Path) -> None:
    from acceptance.loader import load_criteria

    criteria = load_criteria(passing_manifest)
    default_ids = {c.id for c in acceptance_loop.select_criteria(criteria, None)}
    full_ids = {c.id for c in acceptance_loop.select_criteria(criteria, "full")}
    assert default_ids == {"STRUCT-PASS", "BEHAV-TRUE"}
    assert full_ids == {"STRUCT-PASS", "BEHAV-TRUE", "GATED-FULL"}


def test_iterations_below_one_rejected(tmp_path: Path, passing_manifest: Path) -> None:
    exit_code = acceptance_loop.main(
        _loop_args(tmp_path, passing_manifest, iterations=0)
    )
    assert exit_code == 2


def test_missing_manifest_is_setup_error(tmp_path: Path) -> None:
    exit_code = acceptance_loop.main(
        _loop_args(tmp_path, tmp_path / "missing.toml")
    )
    assert exit_code == 2
