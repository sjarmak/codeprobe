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
  agent spend).

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
import re
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
    """
    record: dict[str, Any] = {"criterion_id": action.criterion_id}
    try:
        completed = subprocess.run(
            ["bash", "-c", action.shell_snippet],
            cwd=str(workspace),
            timeout=timeout_s,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        record["outcome"] = "timeout"
        record["detail"] = f"exceeded {timeout_s}s"
        return record
    except OSError as exc:
        record["outcome"] = "os_error"
        record["detail"] = f"{type(exc).__name__}: {exc}"
        return record
    record["outcome"] = "completed"
    record["returncode"] = completed.returncode
    if completed.stderr:
        record["stderr_tail"] = completed.stderr[-2000:]
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
) -> int:
    """Run up to ``iterations`` compile→execute→verify→decide cycles."""
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
        verdict_path = history_dir / f"verdict-{iteration:04d}.json"
        verifier.write_verdict(verdict, verdict_path)

        print(
            f"[iter {iteration}] verdict: status={verdict['status']} "
            f"all_pass={verdict['all_pass']} "
            f"pass={verdict['pass_count']} fail={verdict['fail_count']} "
            f"skip={verdict['skip_count']} "
            f"(mode-skips={verdict['mode_skip_count']}) "
            f"evaluated_pct={verdict['evaluated_pct']}"
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
    args = parser.parse_args(argv)

    if args.iterations < 1:
        print(f"ERROR: --iterations must be >= 1, got {args.iterations}", file=sys.stderr)
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
    )


if __name__ == "__main__":
    sys.exit(main())
