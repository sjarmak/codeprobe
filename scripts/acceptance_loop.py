#!/usr/bin/env python3
"""Acceptance-loop orchestrator: compile → execute → verify → decide.

Runs the behavioral acceptance loop end to end for N iterations:

1. **Compile** — :func:`codeprobe.acceptance_compiler.compile_actions` turns
   the criteria manifest into per-criterion bash snippets (the Test Agent's
   mechanical actions).
2. **Execute** — each snippet runs in a fresh workspace directory via
   ``bash -c`` with an explicit timeout and captured output. Executing
   compiled snippets is structural plumbing, not model reasoning, so plain
   subprocess is ZFC-clean here.
3. **Verify** — :class:`acceptance.verify.Verifier` evaluates the manifest
   against the workspace and produces a verdict.
4. **Persist** — the verdict is written to a durable history directory
   (default ``acceptance/verdict-history/``, gitignored — NOT /tmp), named
   ``verdict-NNNN.json`` by iteration.
5. **Decide** — the verdict feeds
   :class:`acceptance.converge.ConvergenceController`; the loop honors its
   decision (stop on RELEASE and on any HALT/ESCALATE).

This script does NOT auto-fix anything — the Fix Agent stage is out of
scope. Every decision (thresholds, halts, release readiness) stays in the
deterministic ``acceptance/`` policy code; this file is IO, subprocess, and
state only.

Eval modes:

- ``default`` — criteria carrying ``eval_mode_required`` are excluded from
  compilation (the Verifier mode-skips them anyway, so executing their
  snippets would be wasted or costly work). A green verdict in this mode is
  NOT release evidence for mode-gated tiers.
- ``full`` — every criterion compiles and executes, including real
  ``codeprobe mine``/``run`` invocations against ``--target-repo`` (real
  agent spend). Because ``--target-repo`` is shared and persistent across
  iterations (unlike the fresh-per-iteration workspace), ``target_repo/
  .codeprobe`` is wiped at the start of every full-mode iteration — see
  :func:`_reset_target_repo_state` — so a producer that times out or
  crashes cannot leave stale prior-iteration output for a dependent
  statistical check to silently pass on.

History is append-only across invocations: the next iteration number is one
past the highest existing ``verdict-NNNN.json``. To reset the loop, delete
the entire history directory (including ``converge.db``) — never delete
individual files, or the SQLite history and the file history will disagree.

Usage:
    python scripts/acceptance_loop.py --eval-mode default --iterations 1
    python scripts/acceptance_loop.py --eval-mode full --iterations 2

Exit codes:
    0   all requested iterations completed with CONTINUE, or RELEASE reached
    1   the convergence controller halted or escalated
    2   setup error (bad arguments, missing manifest)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT_DEFAULT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT_DEFAULT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT_DEFAULT))

from acceptance.converge import ConvergenceController, Decision  # noqa: E402
from acceptance.loader import Criterion, load_criteria  # noqa: E402
from acceptance.verify import Verifier  # noqa: E402
from codeprobe.acceptance_compiler import TestAction, compile_actions  # noqa: E402

#: Per-action wall-clock timeout in seconds. Actions are compiled snippets
#: (a handful of shell commands each); anything slower is hung, and a hung
#: action must not stall the loop.
DEFAULT_ACTION_TIMEOUT_S: float = 300.0

#: Per-producer-step wall-clock timeout in seconds. A real ``codeprobe run``
#: against a genuine agent can be slow; kept generous but bounded so a hung
#: producer cannot stall the loop forever.
DEFAULT_PRODUCER_TIMEOUT_S: float = 1800.0

#: Number of tasks the producer's ``codeprobe mine`` step requests. Small so
#: the producer stays cheap; the statistical criteria only need >= 1 result.
DEFAULT_PRODUCER_MINE_COUNT: int = 3

#: The ``codeprobe`` invocation the producer shells out to. A module-level
#: constant (not a hardcoded literal) so a test can point the producer at a
#: stub/shim binary — e.g. ``(sys.executable, "-m", "codeprobe")`` — without
#: a real install on PATH.
DEFAULT_CODEPROBE_CMD: tuple[str, ...] = ("codeprobe",)

#: Default durable verdict history directory, repo-relative and gitignored.
DEFAULT_HISTORY_DIRNAME: str = "acceptance/verdict-history"

#: Decisions that stop the loop with a nonzero exit.
_HALT_DECISIONS: frozenset[Decision] = frozenset(
    {
        Decision.HALT_MAX_ITERATIONS,
        Decision.HALT_REGRESSION,
        Decision.HALT_STUCK,
        Decision.ESCALATE,
    }
)

_VERDICT_FILE_RE = re.compile(r"^verdict-(\d{4})\.json$")


def next_iteration(history_dir: Path) -> int:
    """Return one past the highest ``verdict-NNNN.json`` index (1 if none)."""
    highest = 0
    if history_dir.is_dir():
        for entry in history_dir.iterdir():
            match = _VERDICT_FILE_RE.match(entry.name)
            if match:
                highest = max(highest, int(match.group(1)))
    return highest + 1


def select_criteria(criteria: list[Criterion], eval_mode: str | None) -> list[Criterion]:
    """Mechanical mirror of the Verifier's eval-mode gate for compilation.

    Criteria whose ``eval_mode_required`` does not match the current mode are
    mode-skipped by the Verifier, so compiling and executing their snippets
    would be wasted (or, for ``full``-gated real-agent runs, costly) work.
    """
    return [c for c in criteria if c.eval_mode_required in (None, eval_mode)]


def _poison_artifacts(workspace: Path, artifact_paths: tuple[str, ...]) -> None:
    """Delete an action's declared artifacts after a timeout/os_error.

    A compiled snippet's redirection (``( cmd ) > out 2> err``) opens the
    ``.stdout``/``.stderr`` files before the command runs, so a command that
    prints something and then hangs leaves a *partial* artifact behind even
    though it never finished (the ``.exit`` file is never written, since the
    trailing ``echo "$?" > .exit`` never executes). If those partial
    artifacts are left in the workspace, artifact-presence checks
    (``stdout_contains``, ``stderr_contains``, ``cli_help_contains``,
    ``file_exists``) read them as a normal completion and can PASS on a
    command that actually hung — only ``cli_exit_code`` was ever protected,
    because its artifact is written last. Removing every artifact the
    action declared forces every check type to see a missing artifact and
    skip, the same honest-INCOMPLETE path ``cli_exit_code`` already got.
    """
    for rel in artifact_paths:
        candidate = workspace / rel
        try:
            if candidate.is_dir():
                shutil.rmtree(candidate)
            elif candidate.exists() or candidate.is_symlink():
                candidate.unlink()
        except OSError:
            pass


def _reset_target_repo_state(target_repo: Path) -> None:
    """Remove ``target_repo/.codeprobe`` before a full-mode iteration starts.

    ``target_repo`` is shared and persistent across iterations — unlike the
    fresh-per-iteration ``workspace`` — because the real ``codeprobe
    mine``/``run`` invocations know nothing about this loop's workspaces and
    always write to ``target_repo/.codeprobe``. Every criterion in the
    manifest that writes to or reads from that directory carries
    ``eval_mode_required = "full"``, so this reset is called only when
    ``eval_mode == "full"``.

    Without it, a producer (e.g. ``codeprobe mine``) that times out mid-
    iteration leaves the *previous* iteration's ``.codeprobe/`` completely
    untouched. ``_emit_sync_action`` then copies that stale directory into
    the current iteration's fresh workspace, and a dependent statistical
    check (e.g. ``count_ge`` over ``.codeprobe/tasks``) reports an honest-
    looking PASS on data this iteration never produced — a false pass
    through the exact silent-pass-through class this loop exists to catch.
    Wiping the shared directory first forces an honest missing/empty result
    whenever the producer does not run to completion this iteration.
    """
    codeprobe_dir = target_repo / ".codeprobe"
    if not codeprobe_dir.exists():
        return
    try:
        shutil.rmtree(codeprobe_dir)
    except OSError as exc:
        print(
            f"WARNING: failed to reset {codeprobe_dir} before iteration "
            f"(stale state may leak into this iteration's verdict): {exc}",
            file=sys.stderr,
        )


def _aggregate_run_results(target_repo: Path, producer_agent: str) -> dict[str, Any]:
    """Consolidate every per-arm ``runs/<arm>/results.json`` into one file.

    ``codeprobe run`` writes per-configuration results to
    ``.codeprobe/runs/<arm>/results.json`` (``core.experiment.save_config_results``),
    each a ``{"config", "completed", "summary"}`` object. The statistical
    criteria, however, read a single ``.codeprobe/results.json`` keyed by
    ``completed_tasks`` (``$.completed_tasks[*].cost_source`` etc.). This is
    a mechanical transform — it relocates the *real* ``CompletedTask``
    records the agent produced into the path the contract reads, inventing
    no telemetry. ``producer_agent`` is stamped into the file so a consumer
    can see whose run the records came from (stub vs. real agent).

    Returns a small record dict; writes nothing when no per-arm results
    exist (an honest missing ``results.json`` so dependent criteria skip).
    """
    runs_dir = target_repo / ".codeprobe" / "runs"
    completed: list[Any] = []
    arms: list[str] = []
    for results_path in sorted(runs_dir.glob("*/results.json")):
        try:
            data = json.loads(results_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            return {"outcome": "error", "stage": "aggregate", "detail": f"{results_path}: {exc}"}
        records = data.get("completed")
        if isinstance(records, list):
            completed.extend(records)
            arms.append(results_path.parent.name)
    if not completed:
        return {"outcome": "error", "stage": "aggregate", "detail": "no per-arm results found"}
    out_path = target_repo / ".codeprobe" / "results.json"
    out_path.write_text(
        json.dumps(
            {
                "producer_agent": producer_agent,
                "arms": arms,
                "completed_tasks": completed,
            },
            indent=2,
        )
        + "\n"
    )
    return {
        "outcome": "completed",
        "stage": "aggregate",
        "results_path": str(out_path),
        "completed_count": len(completed),
    }


def _run_producer(
    target_repo: Path,
    producer_agent: str,
    *,
    timeout_s: float = DEFAULT_PRODUCER_TIMEOUT_S,
    mine_count: int = DEFAULT_PRODUCER_MINE_COUNT,
    codeprobe_cmd: tuple[str, ...] = DEFAULT_CODEPROBE_CMD,
) -> dict[str, Any]:
    """Populate ``target_repo/.codeprobe/results.json`` via a real run.

    Runs ``codeprobe mine`` then ``codeprobe run --agent <producer_agent>``
    against ``target_repo``, then aggregates the per-arm results into the
    single ``results.json`` the statistical criteria read. The agent is the
    caller's *explicit* ``--producer-agent`` choice — there is no default,
    so a stub can never masquerade as real release evidence by accident.

    Every step's failure is recorded and returned, never masked: a failed
    mine/run leaves no ``results.json``, so the dependent statistical
    criteria skip and the verdict trends to an honest INCOMPLETE rather than
    a false green.
    """
    mine = [
        *codeprobe_cmd, "mine", str(target_repo),
        "--count", str(mine_count),
        "--no-interactive", "--no-llm", "--source", "local",
    ]
    run = [
        *codeprobe_cmd, "run", str(target_repo),
        "--agent", producer_agent,
        "--repeats", "1", "--uncontained",
    ]
    for stage, cmd in (("mine", mine), ("run", run)):
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, trusted inputs
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                cwd=str(target_repo),
            )
        except subprocess.TimeoutExpired:
            return {"outcome": "timeout", "stage": stage, "detail": f"exceeded {timeout_s}s"}
        except OSError as exc:
            return {"outcome": "os_error", "stage": stage, "detail": f"{type(exc).__name__}: {exc}"}
        if completed.returncode != 0:
            return {
                "outcome": "error",
                "stage": stage,
                "detail": f"exit {completed.returncode}: {completed.stderr.strip()[-500:]}",
            }
    return _aggregate_run_results(target_repo, producer_agent)


def execute_action(
    action: TestAction,
    workspace: Path,
    timeout_s: float,
) -> dict[str, Any]:
    """Run one compiled snippet and return a structured execution record.

    Timeouts and nonzero exits are recorded, never masked — the Verifier
    reads whatever artifacts the snippet managed (or failed) to produce, and
    missing artifacts surface as skips that push the verdict toward an
    honest INCOMPLETE.

    The snippet runs in its own process group (``start_new_session=True``)
    so a timeout can kill the whole tree, not just the ``bash`` wrapper: the
    compiler always wraps the real command in a ``( cmd ) > out 2> err``
    subshell, so the command the Test Agent cares about is always a
    grandchild of the ``bash -c`` process, and a plain SIGKILL to that one
    pid leaves grandchildren (e.g. a hung ``codeprobe mine``) running as
    orphans that keep consuming spend and keep writing into shared state
    (like ``target_repo/.codeprobe/``) after the loop has moved on.
    """
    record: dict[str, Any] = {"criterion_id": action.criterion_id}
    try:
        proc = subprocess.Popen(
            ["bash", "-c", action.shell_snippet],
            cwd=str(workspace),
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        record["outcome"] = "os_error"
        record["detail"] = f"{type(exc).__name__}: {exc}"
        _poison_artifacts(workspace, action.artifact_paths)
        return record

    try:
        _stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass  # already exited between the timeout and the kill attempt
        proc.communicate()  # reap; the whole group is dead so this returns promptly
        record["outcome"] = "timeout"
        record["detail"] = f"exceeded {timeout_s}s"
        _poison_artifacts(workspace, action.artifact_paths)
        return record

    record["outcome"] = "completed"
    record["returncode"] = proc.returncode
    if stderr:
        record["stderr_tail"] = stderr[-2000:]
    return record


def run_loop(
    repo_root: Path,
    criteria_path: Path,
    history_dir: Path,
    workspace_root: Path,
    target_repo: Path,
    eval_mode: str | None,
    iterations: int,
    action_timeout_s: float,
    producer_agent: str | None = None,
    producer_timeout_s: float = DEFAULT_PRODUCER_TIMEOUT_S,
) -> int:
    """Run up to ``iterations`` compile→execute→verify→decide cycles.

    In ``full`` eval mode ``producer_agent`` is required: after resetting
    ``target_repo/.codeprobe`` and before compiling the Test Agent actions,
    the loop runs a real ``codeprobe mine``/``run`` producer with that agent
    so the statistical criteria (``SILENT-RUN-RESULTS-002``, the ``TELEM-``
    cost checks) have a populated ``results.json`` to evaluate instead of
    skipping. The producer agent name is stamped into every full-mode
    verdict so a stub-produced green is never mistaken for real evidence.
    """
    if not criteria_path.is_file():
        print(f"ERROR: criteria manifest not found: {criteria_path}", file=sys.stderr)
        return 2

    history_dir.mkdir(parents=True, exist_ok=True)
    workspace_root.mkdir(parents=True, exist_ok=True)
    controller = ConvergenceController(history_dir / "converge.db")
    criteria = load_criteria(criteria_path)
    active = select_criteria(criteria, eval_mode)
    base_iteration = next_iteration(history_dir)

    mode_label = eval_mode or "default"
    print(
        f"acceptance loop: {len(active)}/{len(criteria)} criteria active "
        f"in eval_mode={mode_label}, starting at iteration {base_iteration}"
    )

    for offset in range(iterations):
        iteration = base_iteration + offset
        workspace = workspace_root / f"iter-{iteration:04d}"
        workspace.mkdir(parents=True, exist_ok=False)

        if eval_mode == "full":
            _reset_target_repo_state(target_repo)
            if producer_agent is not None:
                producer_record = _run_producer(
                    target_repo,
                    producer_agent,
                    timeout_s=producer_timeout_s,
                )
                if producer_record["outcome"] == "completed":
                    print(
                        f"[iter {iteration}] producer ({producer_agent}): "
                        f"populated results.json with "
                        f"{producer_record.get('completed_count')} completed task(s)"
                    )
                else:
                    print(
                        f"[iter {iteration}] producer ({producer_agent}) did not "
                        f"complete: {producer_record['stage']} "
                        f"{producer_record['outcome']} ({producer_record['detail']}) "
                        "— statistical criteria will skip and the verdict trends "
                        "to INCOMPLETE",
                        file=sys.stderr,
                    )

        actions = compile_actions(
            active,
            target_repo=target_repo,
            workspace=workspace,
            project_root=repo_root,
        )
        print(f"[iter {iteration}] executing {len(actions)} compiled action(s)")
        records = [
            execute_action(action, workspace, action_timeout_s) for action in actions
        ]
        for record in records:
            if record["outcome"] != "completed":
                print(
                    f"[iter {iteration}] action {record['criterion_id']}: "
                    f"{record['outcome']} ({record['detail']})",
                    file=sys.stderr,
                )

        verifier = Verifier(
            criteria_path=criteria_path,
            project_root=repo_root,
            eval_mode=eval_mode,
        )
        verdict = verifier.run(workspace, iteration=iteration)
        # Honesty guard (codeprobe-2s54): stamp whose producer run the
        # statistical criteria were evaluated against. A stub producer
        # ("e2e-stub") emits honest-but-fake telemetry (cost_source=
        # "unavailable", cost_usd=0.0) that passes the TELEM-* criteria, so
        # a green verdict is only real release evidence when producer_agent
        # names a REAL agent. None in default mode (no producer ran).
        verdict["producer_agent"] = producer_agent if eval_mode == "full" else None
        verdict_path = history_dir / f"verdict-{iteration:04d}.json"
        verifier.write_verdict(verdict, verdict_path)

        print(
            f"[iter {iteration}] verdict: status={verdict['status']} "
            f"all_pass={verdict['all_pass']} "
            f"pass={verdict['pass_count']} fail={verdict['fail_count']} "
            f"skip={verdict['skip_count']} "
            f"(eval-mode-skips={verdict['mode_skip_count']} "
            f"no-handler-skips={verdict['no_handler_count']}) "
            f"evaluated_pct={verdict['evaluated_pct']}"
        )
        if verdict["no_handler_count"]:
            critical_no_handler = [
                c
                for c in verdict["no_handler_criteria"]
                if c["severity"] in ("critical", "high")
            ]
            print(
                f"[iter {iteration}]   WARNING: {verdict['no_handler_count']} "
                "criterion/a have no registered Verifier handler and are "
                "structurally unevaluable in ANY eval mode "
                f"({[c['criterion_id'] for c in verdict['no_handler_criteria']]}); "
                f"{len(critical_no_handler)} of those are critical/high severity.",
                file=sys.stderr,
            )
        for failure in verdict["failures"]:
            print(
                f"[iter {iteration}]   FAIL {failure['criterion_id']} "
                f"({failure['severity']}): {failure['evidence']}"
            )
        print(f"[iter {iteration}] verdict written: {verdict_path}")

        controller.record_verdict(verdict)
        result = controller.decide()
        print(f"[iter {iteration}] decision: {result.decision.value} — {result.reason}")

        if result.decision in _HALT_DECISIONS:
            print(controller.get_escalation_report(), file=sys.stderr)
            return 1
        if result.decision is Decision.RELEASE:
            return 0

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the acceptance loop: compile criteria into Test Agent "
            "actions, execute them, verify, persist the verdict, and honor "
            "the convergence decision."
        )
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=2,
        help=(
            "Iterations to run this invocation (default: 2 — the minimum "
            "that can produce the two-consecutive-green release evidence)."
        ),
    )
    parser.add_argument(
        "--eval-mode",
        choices=["default", "full"],
        default="default",
        help=(
            "default: mode-gated criteria are excluded (no real agent runs; "
            "a green here is NOT release evidence for mode-gated tiers). "
            "full: every criterion runs, including real codeprobe "
            "mine/run invocations (real agent spend)."
        ),
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=None,
        help=(
            "Root for per-iteration scratch workspaces (default: a fresh "
            "temp directory). Verdicts do NOT live here — see --history-dir."
        ),
    )
    parser.add_argument(
        "--history-dir",
        type=Path,
        default=None,
        help=(
            "Durable verdict history directory (default: "
            f"<repo-root>/{DEFAULT_HISTORY_DIRNAME}). Read later by "
            "scripts/pre_tag_check.py."
        ),
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT_DEFAULT,
        help="Repo root (default: parent of scripts/).",
    )
    parser.add_argument(
        "--criteria",
        type=Path,
        default=None,
        help="Criteria manifest (default: <repo-root>/acceptance/criteria.toml).",
    )
    parser.add_argument(
        "--target-repo",
        type=Path,
        default=None,
        help=(
            "Repository the compiled {repo} commands run against "
            "(default: --repo-root)."
        ),
    )
    parser.add_argument(
        "--action-timeout",
        type=float,
        default=DEFAULT_ACTION_TIMEOUT_S,
        help=f"Per-action timeout in seconds (default: {DEFAULT_ACTION_TIMEOUT_S}).",
    )
    parser.add_argument(
        "--producer-agent",
        type=str,
        default=None,
        help=(
            "Agent that produces the real run this loop verifies. REQUIRED "
            "with --eval-mode full — the statistical criteria need a "
            "results.json populated by a genuine 'codeprobe run', and the "
            "agent must be a conscious operator choice (no silent default). "
            "Use 'e2e-stub' for a zero-budget wiring check; the two full-mode "
            "greens gating a release MUST come from a REAL agent (e.g. "
            "'claude'), never the stub."
        ),
    )
    parser.add_argument(
        "--producer-timeout",
        type=float,
        default=DEFAULT_PRODUCER_TIMEOUT_S,
        help=f"Per-producer-step timeout in seconds (default: {DEFAULT_PRODUCER_TIMEOUT_S}).",
    )
    args = parser.parse_args(argv)

    if args.iterations < 1:
        print(f"ERROR: --iterations must be >= 1, got {args.iterations}", file=sys.stderr)
        return 2

    if args.eval_mode == "full" and not args.producer_agent:
        print(
            "ERROR: --producer-agent is required with --eval-mode full. The "
            "statistical tier is only meaningful against a results.json a real "
            "'codeprobe run' produced; pick the agent explicitly (e.g. "
            "--producer-agent claude, or --producer-agent e2e-stub for a "
            "zero-budget wiring check).",
            file=sys.stderr,
        )
        return 2

    repo_root = args.repo_root.resolve()
    criteria_path = (
        args.criteria if args.criteria is not None
        else repo_root / "acceptance" / "criteria.toml"
    )
    history_dir = (
        args.history_dir if args.history_dir is not None
        else repo_root / DEFAULT_HISTORY_DIRNAME
    )
    workspace_root = (
        args.workspace_root
        if args.workspace_root is not None
        else Path(tempfile.mkdtemp(prefix="codeprobe-acceptance-"))
    )
    target_repo = args.target_repo if args.target_repo is not None else repo_root
    eval_mode = None if args.eval_mode == "default" else args.eval_mode

    return run_loop(
        repo_root=repo_root,
        criteria_path=Path(criteria_path).resolve(),
        history_dir=Path(history_dir).resolve(),
        workspace_root=Path(workspace_root).resolve(),
        target_repo=Path(target_repo).resolve(),
        eval_mode=eval_mode,
        iterations=args.iterations,
        action_timeout_s=args.action_timeout,
        producer_agent=args.producer_agent,
        producer_timeout_s=args.producer_timeout,
    )


if __name__ == "__main__":
    sys.exit(main())
