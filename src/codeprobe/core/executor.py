"""Task execution — run agents on tasks and collect results."""

from __future__ import annotations

import contextlib
import dataclasses
import inspect
import json as _json
import logging
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from codeprobe.adapters.protocol import AdapterQuotaError
from codeprobe.core.checkpoint import CheckpointStore
from codeprobe.core.events import (
    BudgetChecker,
    EventDispatcher,
    RunFinished,
    RunStarted,
    TaskScored,
    TaskStarted,
)
from codeprobe.core.isolation import (
    IsolationStrategy,
    WorktreeIsolation,
    cleanup_multi_repo_workspace,
    git_pin_commit,
    git_restore_clean,
    quarantine_local_source,
    quarantine_sibling_experiments,
    setup_multi_repo_workspace,
)
from codeprobe.core.preamble import (
    PreambleResolver,
    base_prompt,
    compose_instruction,
    task_preamble_context,
)
from codeprobe.core.scoring import (
    COPYTREE_IGNORE,
    AgentState,
    Scorer,
    ScoreResult,
    get_scorer,
    read_task_metadata,
    sanitize_secrets,
    scorer_accepts_agent_state,
    scorer_env_override,
)
from codeprobe.core.turn_cap import (
    resolve_turn_cap,
    resolve_turn_cap_family,
)
from codeprobe.models.experiment import CompletedTask, ExperimentConfig

if TYPE_CHECKING:
    from codeprobe.adapters.protocol import AgentAdapter, AgentConfig, AgentOutput
    from codeprobe.trace.recorder import TraceRecorder


# Per-run agent artifacts that must not leak across task runs.
_STALE_ANSWER_FILES = ("answer.txt", "answer.json", "reward.txt")
_MCP_INSTRUCTION_VARIANT = "instruction_mcp.md"


def _drop_stale_answers(base: Path) -> None:
    """Remove any leftover agent artifacts under *base*."""
    for name in _STALE_ANSWER_FILES:
        (base / name).unlink(missing_ok=True)


@dataclass(frozen=True)
class DryRunEstimate:
    """Resource estimate for a dry-run (no agents spawned)."""

    total_tasks: int
    total_configs: int
    total_runs: int
    max_concurrent: int
    estimated_disk_mb: float
    estimated_cost_range: tuple[float, float]


def _estimate_repo_size_mb(repo_path: Path) -> float:
    """Estimate the on-disk size of a repo in megabytes.

    Uses ``du -sm`` for speed; falls back to a conservative default.
    """
    try:
        result = subprocess.run(
            ["du", "-sm", str(repo_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return float(result.stdout.split()[0])
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        pass
    return 100.0  # conservative default


def dry_run_estimate(
    *,
    task_count: int,
    configs_count: int,
    repeats: int,
    parallel: int,
    repo_path: Path,
) -> DryRunEstimate:
    """Compute resource estimates without spawning any agents.

    Returns a frozen dataclass with counts, concurrency, disk, and cost
    projections.
    """
    total_runs = task_count * configs_count * repeats
    max_concurrent = min(parallel, total_runs)
    repo_mb = _estimate_repo_size_mb(repo_path)
    # Each parallel worker needs its own worktree copy
    estimated_disk_mb = repo_mb * max_concurrent

    # Cost heuristic: $0.02 - $0.15 per run (typical for light coding tasks)
    cost_low = total_runs * 0.02
    cost_high = total_runs * 0.15

    return DryRunEstimate(
        total_tasks=task_count,
        total_configs=configs_count,
        total_runs=total_runs,
        max_concurrent=max_concurrent,
        estimated_disk_mb=round(estimated_disk_mb, 1),
        estimated_cost_range=(round(cost_low, 2), round(cost_high, 2)),
    )


# Global concurrency semaphore — caps total active agent subprocesses
# across all executor instances in the process.
_global_semaphore: threading.Semaphore | None = None
_semaphore_lock = threading.Lock()


def set_max_concurrency(max_concurrent: int) -> None:
    """Set the global concurrency cap for agent subprocesses."""
    global _global_semaphore  # noqa: PLW0603
    with _semaphore_lock:
        _global_semaphore = threading.Semaphore(max_concurrent)


def get_concurrency_semaphore() -> threading.Semaphore | None:
    """Return the global semaphore (None if not configured)."""
    return _global_semaphore


logger = logging.getLogger(__name__)


def _find_active_experiment_dir(
    task_dirs: list[Path],
    repo_path: Path,
) -> Path | None:
    """Return the closest ancestor of *task_dirs[0]* containing experiment.json.

    Walks up from the first task directory toward *repo_path* until it finds a
    directory with an ``experiment.json``.  Returns ``None`` when no such
    ancestor is reachable inside the repo (e.g. tests that bypass the standard
    ``.codeprobe/`` layout) — callers should treat ``None`` as "skip
    quarantine."
    """
    if not task_dirs:
        return None
    try:
        repo_resolved = repo_path.resolve()
        cur = task_dirs[0].resolve().parent
    except OSError:
        return None
    while True:
        if (cur / "experiment.json").is_file():
            return cur
        if cur == repo_resolved or cur == cur.parent:
            return None
        cur = cur.parent


def _classify_error(exc: BaseException) -> str:
    """Classify an exception into an error category.

    Returns one of: 'quota', 'timeout', 'system', 'agent'.
    """
    if isinstance(exc, AdapterQuotaError):
        return "quota"
    if isinstance(exc, subprocess.TimeoutExpired):
        return "timeout"
    if isinstance(exc, (OSError, MemoryError)):
        return "system"
    return "agent"


def _build_session_namespace(config_label: str) -> str:
    """Return a unique namespace for one config execution."""
    return f"{config_label}-{uuid.uuid4().hex[:8]}"


def _call_isolate_session(
    adapter: AgentAdapter,
    slot_id: int,
    *,
    namespace: str | None = None,
    pristine: bool = False,
) -> dict[str, str]:
    """Call ``adapter.isolate_session`` forwarding only supported kwargs.

    ``namespace`` and ``pristine`` are passed when the adapter's signature
    accepts them; adapters with the bare ``(slot_id)`` shape keep working.
    """
    isolate = getattr(adapter, "isolate_session")
    try:
        params: Mapping[str, inspect.Parameter] = inspect.signature(isolate).parameters
    except (TypeError, ValueError):
        params = {}
    kwargs: dict[str, str | bool | None] = {}
    if "namespace" in params:
        kwargs["namespace"] = namespace
    if "pristine" in params:
        kwargs["pristine"] = pristine
    result: dict[str, str] = isolate(slot_id, **kwargs)
    return result


def _cleanup_session_namespace(
    adapter: AgentAdapter,
    namespace: str | None,
) -> None:
    """Best-effort cleanup for adapter-specific session temp dirs."""
    cleanup = getattr(adapter, "cleanup_session_namespace", None)
    if not callable(cleanup):
        return
    cleanup(namespace)


@dataclass(frozen=True)
class TaskResult:
    """Completed task plus raw agent output for trace storage."""

    completed: CompletedTask
    agent_stdout: str = ""
    agent_stderr: str = ""


def load_instruction(task_dir: Path, variant: str | None = None) -> str:
    """Read the instruction file from a task directory.

    Falls back to instruction.md if the variant file doesn't exist.
    Raises FileNotFoundError if no instruction file is found.
    """
    if variant:
        variant_path = (task_dir / variant).resolve()
        if not str(variant_path).startswith(str(task_dir.resolve())):
            raise ValueError(f"instruction_variant escapes task directory: {variant!r}")
        if variant_path.is_file():
            return variant_path.read_text(encoding="utf-8").strip()

    default_path = task_dir / "instruction.md"
    if default_path.is_file():
        return default_path.read_text(encoding="utf-8").strip()

    raise FileNotFoundError(f"No instruction file found in {task_dir}")


def resolve_instruction_variant(
    task_dir: Path,
    *,
    variant: str | None = None,
    mcp_config: dict | None = None,
) -> str | None:
    """Return the effective instruction variant for *task_dir*.

    Explicit config wins. For MCP-enabled configs over mined tasks, default to
    ``instruction_mcp.md`` when that task-specific prompt exists.
    """
    if variant is not None:
        return variant
    if not mcp_config:
        return None
    if (task_dir / _MCP_INSTRUCTION_VARIANT).is_file():
        return _MCP_INSTRUCTION_VARIANT
    return None


def _build_scoring_details(score_result: ScoreResult) -> dict:
    """Project a ScoreResult into the CompletedTask.scoring_details dict.

    Keeps the backward-compatible passed/error fields and surfaces the voxa
    contract (scorer_family, sub_scores) plus the Slice 1b verdict /
    materialized_via / diagnostics so aggregate reporting can tell agent
    failure from verifier-infrastructure failure. Extracted from
    ``execute_task`` so the scoring projection is independently testable
    (codeprobe-s6o).
    """
    details: dict = {"passed": score_result.passed, "error": score_result.error}
    if score_result.details:
        details.update(dict(score_result.details))
    if score_result.scorer_family:
        details["scorer_family"] = score_result.scorer_family
    if score_result.sub_scores:
        details["sub_scores"] = dict(score_result.sub_scores)
    if score_result.verdict is not None:
        details["verdict"] = score_result.verdict
    details["materialized_via"] = score_result.materialized_via
    if score_result.diagnostics:
        details["diagnostics"] = dict(score_result.diagnostics)
    return details


def _score_in_sandbox(
    *,
    task_id: str,
    task_dir: Path,
    output: AgentOutput,
    scorer: Scorer,
    found_answer: Path | None,
    found_answer_json: Path | None,
    dual_mode: bool,
    effective_wt: Path | None,
    base_commit: str | None,
    effective_workspace: Path,
    turn_cap_meta: dict,
    output_fields: dict,
    resolved_preambles: list[dict[str, str]],
) -> TaskResult:
    """Score the agent output in an isolated per-run sandbox.

    Snapshots the task files (and any agent-produced answer artifacts) into a
    fresh temp dir so concurrent runs never share mutable scoring state, runs
    the scorer, and projects the result into a ``TaskResult``. Extracted from
    ``execute_task`` (codeprobe-s6o, HIGH #3) so the scoring stage is a named,
    independently-testable unit. Behaviour is unchanged.
    """
    with tempfile.TemporaryDirectory(prefix=f"codeprobe-score-{task_id}-") as _tmp:
        scoring_dir = Path(_tmp) / task_id
        try:
            shutil.copytree(
                task_dir,
                scoring_dir,
                symlinks=True,
                ignore=shutil.ignore_patterns(*COPYTREE_IGNORE),
            )
        except OSError as exc:
            return TaskResult(
                completed=CompletedTask(
                    task_id=task_id,
                    automated_score=0.0,
                    status="error",
                    metadata={
                        "error": f"Failed to snapshot task dir: {exc}",
                        **turn_cap_meta,
                    },
                    **output_fields,
                ),
                agent_stdout=output.stdout,
                agent_stderr=output.stderr or "",
            )

        # Drop any stale answer files copied from the source task dir
        # — we only want the current run's artifacts in the sandbox.
        _drop_stale_answers(scoring_dir)

        artifact_copy_error: str | None = None
        if found_answer is not None:
            try:
                shutil.copy2(found_answer, scoring_dir / "answer.txt")
            except OSError as exc:
                artifact_copy_error = (
                    f"failed to stage answer.txt from {found_answer}: {exc}"
                )
        if found_answer_json is not None and artifact_copy_error is None:
            try:
                shutil.copy2(found_answer_json, scoring_dir / "answer.json")
            except OSError as exc:
                artifact_copy_error = (
                    f"failed to stage answer.json from {found_answer_json}: {exc}"
                )

        # In dual mode the artifact leg is load-bearing for scoring;
        # a missing copy would silently fall through to a 0-score
        # artifact result that default/weighted policy can still
        # clamp into a pass. Fail closed instead.
        if dual_mode and artifact_copy_error is not None:
            return TaskResult(
                completed=CompletedTask(
                    task_id=task_id,
                    automated_score=0.0,
                    status="error",
                    metadata={"error": artifact_copy_error, **turn_cap_meta},
                    **output_fields,
                ),
                agent_stdout=output.stdout,
                agent_stderr=output.stderr or "",
            )

        # Bind TASK_REPO_ROOT so a dual task's ``tests/test.sh`` cd's
        # into the per-run worktree instead of the shared mined
        # ``repo_path`` fallback. Non-dual runs and runs without an
        # owned worktree see no override.
        env_overrides: dict[str, str] | None = None
        if effective_wt is not None:
            env_overrides = {"TASK_REPO_ROOT": str(effective_wt)}

        # When eligible (single-repo, no source-quarantine, git
        # workspace), pass the captured base_commit to scorers that
        # accept it so the verifier runs against a fresh checkout
        # with the agent's full diff materialised (Slice 1b —
        # codeprobe-xysn). Support is detected structurally — any
        # scorer opting into the ``agent_state`` kwarg gets it;
        # the rest keep the legacy in_place behavior.
        agent_state: AgentState | None = None
        if base_commit is not None and scorer_accepts_agent_state(scorer):
            agent_state = AgentState(
                base_commit=base_commit, workspace=effective_workspace
            )

        score_kwargs: dict[str, AgentState] = (
            {"agent_state": agent_state} if agent_state is not None else {}
        )
        with scorer_env_override(env_overrides):
            score_result = scorer.score(output.stdout, scoring_dir, **score_kwargs)

    metadata: dict = dict(turn_cap_meta)
    if resolved_preambles:
        metadata["resolved_preambles"] = resolved_preambles

    return TaskResult(
        completed=CompletedTask(
            task_id=task_id,
            automated_score=score_result.score,
            status="completed",
            scoring_details=_build_scoring_details(score_result),
            metadata=metadata,
            **output_fields,
        ),
        agent_stdout=output.stdout,
        agent_stderr=output.stderr or "",
    )


def execute_task(
    adapter: AgentAdapter,
    task_dir: Path,
    repo_path: Path,
    agent_config: AgentConfig,
    instruction_variant: str | None = None,
    reward_type: str = "binary",
    preamble_names: tuple[str, ...] = (),
    preamble_resolver: PreambleResolver | None = None,
    worktree_path: Path | None = None,
    session_env: dict[str, str] | None = None,
    dual_worktree_factory: Callable[[Path, str], IsolationStrategy] | None = None,
    hide_local_source: Literal["off", "hide", "scaffold"] = "off",
    hide_local_source_keep: tuple[str, ...] = (),
    config_max_turns_source: str = "",
) -> TaskResult:
    """Execute a single task and return a TaskResult with trace data.

    Never raises — errors are captured in the result metadata.

    When ``task.verification.verification_mode == 'dual'`` (read from the
    task's ``metadata.json``) the executor forces ``reward_type='dual'`` and
    binds a per-run worktree plus a per-run scoring sandbox so parallel
    runs of the same task never share mutable state.
    """
    task_id = task_dir.name

    # Load task metadata once — used for reward_type auto-detection and
    # preamble context (e.g. sg_repo for Sourcegraph preamble).
    _task_meta = read_task_metadata(task_dir)
    _verification = _task_meta.get("verification") or {}
    effective_instruction_variant = resolve_instruction_variant(
        task_dir,
        variant=instruction_variant,
        mcp_config=agent_config.mcp_config,
    )

    # Verification-mode override — top level and unconditional. A task whose
    # metadata declares ``verification_mode == 'dual'`` forces the dual
    # scorer regardless of the reward_type configured on the experiment;
    # this is NOT nested inside the "binary" auto-detect block because a
    # continuous-reward experiment can still carry a dual task.
    if _verification.get("verification_mode") == "dual":
        reward_type = "dual"

    # Auto-detect reward_type from task metadata when caller uses default.
    # Oracle tasks (org-scale) need "continuous" scoring to read reward.txt;
    # the default "binary" would score exit-code-only and always pass.
    if reward_type == "binary":
        task_rt = _verification.get("reward_type")
        if task_rt and task_rt != "binary":
            reward_type = task_rt

    # Task-category-aware turn cap (codeprobe-gg9f). Precedence:
    # CLI / experiment.json > task.max_turns_override > per-family default.
    # A global cap collapses SDLC reward (codeprobe-aupz) so SDLC is left
    # uncapped while oracle_checks keeps a tight cap. Inject the resolved
    # value into a fresh AgentConfig so the adapter sees the per-trial cap,
    # and record the choice + source on every result envelope so cap-retune
    # analysis can verify it — including the error_max_turns trials, which
    # exit through the agent-error path below rather than scoring.
    _meta_block = _task_meta.get("metadata") or {}
    _raw_override = _meta_block.get("max_turns_override")
    _task_override = (
        _raw_override
        if isinstance(_raw_override, int)
        and not isinstance(_raw_override, bool)
        and _raw_override > 0
        else None
    )
    _turn_cap = resolve_turn_cap(
        config_max_turns=agent_config.max_turns,
        config_source=config_max_turns_source,
        task_override=_task_override,
        family=resolve_turn_cap_family(_meta_block, _verification),
    )
    _turn_cap_meta = {
        "max_turns_chosen": _turn_cap.max_turns,
        "max_turns_source": _turn_cap.source,
    }
    if _turn_cap.max_turns != agent_config.max_turns:
        agent_config = dataclasses.replace(
            agent_config, max_turns=_turn_cap.max_turns
        )

    # NOTE: task_dir is intentionally never mutated here. Stale agent
    # artifacts are removed inside the per-run scoring sandbox (after the
    # snapshot copytree) so concurrent runs can't race on the shared
    # task_dir and fixture files are never destroyed.

    def _error_result(error: str, error_category: str | None = None) -> TaskResult:
        return TaskResult(
            completed=CompletedTask(
                task_id=task_id,
                automated_score=0.0,
                status="error",
                error_category=error_category,
                metadata={"error": error, **_turn_cap_meta},
            ),
        )

    # Per-run worktree for dual-mode tasks. Mined test.sh scripts hardcode
    # ``cd {repo_path}`` to the original repo, so two parallel runs of the
    # same dual task would trample each other's workspace state. Bind a
    # dedicated worktree slot from the isolation pool when the caller
    # didn't already supply one.
    _owned_dual_iso: IsolationStrategy | None = None
    _owned_dual_wt: Path | None = None
    if reward_type == "dual" and worktree_path is None:
        try:
            if dual_worktree_factory is not None:
                _owned_dual_iso = dual_worktree_factory(
                    repo_path, f"dual-{task_id}-{uuid.uuid4().hex[:8]}"
                )
            else:
                _owned_dual_iso = WorktreeIsolation(
                    repo_path,
                    pool_size=1,
                    namespace=f"dual-{task_id}-{uuid.uuid4().hex[:8]}",
                )
            _owned_dual_wt = _owned_dual_iso.acquire()
        except (subprocess.CalledProcessError, OSError, ValueError) as exc:
            # Roll back a half-built isolation before bailing.
            if _owned_dual_iso is not None:
                try:
                    _owned_dual_iso.cleanup()
                except Exception:  # pragma: no cover — defensive
                    pass
            return _error_result(
                f"Failed to acquire dual-mode worktree: {exc}",
                error_category="system",
            )

    # Effective worktree: caller-provided > owned dual worktree > None.
    _effective_wt: Path | None = worktree_path or _owned_dual_wt

    try:
        try:
            instruction = load_instruction(
                task_dir,
                variant=effective_instruction_variant,
            )
        except FileNotFoundError as exc:
            return _error_result(str(exc))

        resolved_preambles: list[dict[str, str]] = []
        if preamble_names and preamble_resolver is None:
            return _error_result(
                f"preambles={preamble_names!r} requested but no "
                "preamble_resolver provided"
            )

        if preamble_names and preamble_resolver is not None:
            try:
                extra_ctx = task_preamble_context(
                    _task_meta,
                    preamble_names=preamble_names,
                    task_id=task_id,
                )
            except ValueError as exc:
                return _error_result(f"Preamble resolution failed: {exc}")

            try:
                prompt, resolved_preambles = compose_instruction(
                    instruction,
                    repo_path,
                    preamble_names=list(preamble_names),
                    resolver=preamble_resolver,
                    task_id=task_id,
                    worktree_path=_effective_wt,
                    extra_context=extra_ctx or None,
                )
            except (FileNotFoundError, ValueError) as exc:
                return _error_result(f"Preamble resolution failed: {exc}")
        else:
            prompt = base_prompt(instruction, repo_path, worktree_path=_effective_wt)

        # Pin workspace to pre-merge commit when task has a ground_truth_commit.
        # The agent starts from the parent of the merge commit (the state before
        # the PR landed) and must reproduce the changes.
        pin_commit = (_task_meta.get("metadata") or {}).get("ground_truth_commit", "")
        effective_workspace = _effective_wt or repo_path
        if pin_commit:
            try:
                git_pin_commit(effective_workspace, f"{pin_commit}^")
                logger.info(
                    "[%s] Pinned workspace to %s^ (pre-merge state)",
                    task_id,
                    pin_commit[:8],
                )
            except subprocess.CalledProcessError as exc:
                return _error_result(
                    f"Failed to pin workspace to {pin_commit[:8]}^: "
                    + (exc.stderr.decode(errors="replace") if exc.stderr else str(exc)),
                    error_category="system",
                )

        # Cross-repo tasks: lay out additional repos as workspace/repos/<name>
        # and pin each to its own ground_truth_commit^.  Primary repo keeps
        # its existing location so single-repo tasks are unaffected.
        additional_repos = (_task_meta.get("metadata") or {}).get(
            "additional_repos", []
        )
        if additional_repos:
            try:
                setup_multi_repo_workspace(effective_workspace, additional_repos)
                logger.info(
                    "[%s] Set up %d additional repo(s) under %s/repos/",
                    task_id,
                    len(additional_repos),
                    effective_workspace,
                )
            except (
                subprocess.CalledProcessError,
                OSError,
                ValueError,
                TypeError,
            ) as exc:
                stderr = ""
                if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
                    stderr = exc.stderr.decode(errors="replace")
                return _error_result(
                    f"Failed to set up multi-repo workspace: {stderr or exc}",
                    error_category="system",
                )

        # Capture base_commit BEFORE adapter.run so the scorer can later
        # materialise a clean checkout at that SHA (Slice 1b — bead
        # codeprobe-xysn). Eligible when single-repo, no source-quarantine
        # active, and the workspace IS a git repo. Multi-repo and
        # scaffold/hide modes intentionally fall through to in_place
        # because:
        #   * a single SHA cannot honestly represent N additional_repos,
        #     each pinned to its own ground_truth_commit;
        #   * scaffold/hide mode mutates the workspace under
        #     ``source_ctx`` and the diff would include overlay artifacts.
        base_commit: str | None = None
        materialise_eligible = (
            hide_local_source == "off"
            and not additional_repos
            and (effective_workspace / ".git").exists()
        )
        if materialise_eligible:
            try:
                rev = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=effective_workspace,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=10,
                )
                base_commit = rev.stdout.strip() or None
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
                # Workspace looked git-ish but rev-parse failed (corrupt
                # repo, unborn HEAD). Fall through to in_place rather
                # than crash the run. Warning-level so the degradation
                # is visible without flipping the whole run to DEBUG.
                logger.warning(
                    "[%s] base_commit capture failed; falling back to in_place",
                    task_id,
                    exc_info=True,
                )

        # Optional file-removal isolation (codeprobe-jf28).  When enabled,
        # local source files are stashed for the duration of the agent
        # run so the agent has no choice but to use Sourcegraph MCP.
        # ``effective_workspace`` is the per-trial worktree (when
        # parallel) or ``repo_path`` (single-tenant).  On context exit
        # the source is restored before scoring runs.
        #
        # ``hide_local_source`` selects ``"off"`` (default; no isolation),
        # ``"hide"`` (jf28 behaviour; workspace appears empty) or
        # ``"scaffold"`` (codeprobe-2nw2; workspace shows 0-byte
        # placeholders at tracked extensions and agent edits get overlaid
        # on top of restored source so scoring sees the merged tree).
        source_ctx = (
            quarantine_local_source(
                effective_workspace,
                keep=hide_local_source_keep,
                mode=hide_local_source,
            )
            if hide_local_source != "off"
            else contextlib.nullcontext()
        )

        try:
            with source_ctx:
                output = adapter.run(prompt, agent_config, session_env=session_env)
        except subprocess.TimeoutExpired as exc:
            return _error_result(
                sanitize_secrets(str(exc)),
                error_category="timeout",
            )
        except Exception as exc:
            return _error_result(
                sanitize_secrets(str(exc)),
                error_category=_classify_error(exc),
            )

        def _output_fields() -> dict:
            return dict(
                duration_seconds=output.duration_seconds,
                input_tokens=output.input_tokens,
                output_tokens=output.output_tokens,
                cache_read_tokens=output.cache_read_tokens,
                cache_creation_tokens=output.cache_creation_tokens,
                cost_usd=output.cost_usd,
                cost_model=output.cost_model,
                cost_source=output.cost_source,
                tool_call_count=output.tool_call_count,
                tool_use_by_name=output.tool_use_by_name,
                num_turns=output.num_turns,
                result_subtype=output.result_subtype,
                duration_api_ms=output.duration_api_ms,
                mcp_init=output.mcp_init.to_dict()
                if output.mcp_init is not None
                else None,
            )

        # For oracle tasks, the agent writes answer.txt / answer.json to the
        # workspace root. Locate any such artifacts now; the actual copy
        # into the scoring sandbox happens below so the ORIGINAL task_dir is
        # never mutated by scoring. In dual mode the effective workspace is
        # authoritative — we never fall back to ``repo_path`` because a
        # stale file from another run or manual testing could silently
        # leak in and pass the artifact leg.
        dual_mode = reward_type == "dual"
        effective_repo = _effective_wt or repo_path
        allow_repo_fallback = _effective_wt is not None and not dual_mode

        found_answer: Path | None = None
        if (effective_repo / "answer.txt").is_file():
            found_answer = effective_repo / "answer.txt"
        elif allow_repo_fallback and (repo_path / "answer.txt").is_file():
            found_answer = repo_path / "answer.txt"

        found_answer_json: Path | None = None
        if (effective_repo / "answer.json").is_file():
            found_answer_json = effective_repo / "answer.json"
        elif allow_repo_fallback and (repo_path / "answer.json").is_file():
            found_answer_json = repo_path / "answer.json"

        # If the agent failed with no output AND no answer file was produced,
        # return an error. But if an answer exists (e.g. agent timed out
        # after writing it), fall through to scoring.
        has_answer = found_answer is not None or found_answer_json is not None
        if output.exit_code != 0 and not output.stdout.strip() and not has_answer:
            # A bare timeout (or quota stub) lands here with empty stdout:
            # honour the adapter-declared category and error text so the
            # row is never miscounted as an agent failure.
            error_msg = (
                output.error
                or output.stderr
                or f"Agent exited with code {output.exit_code}"
            )
            return TaskResult(
                completed=CompletedTask(
                    task_id=task_id,
                    automated_score=0.0,
                    status="error",
                    error_category=output.error_category or "agent",
                    metadata={"error": sanitize_secrets(error_msg), **_turn_cap_meta},
                    **_output_fields(),
                ),
                agent_stdout=output.stdout,
                agent_stderr=output.stderr or "",
            )

        # Adapter-reported structured error (e.g. Claude CLI is_error=true,
        # auth/API failure, max_turns without artifact). The CLI tucks the
        # error text inside its JSON envelope, so stdout is non-empty and
        # the exit-code guard above does not fire.  When no artifact exists
        # we must short-circuit — scoring a workspace the agent never
        # actually touched yields vacuous pass/fail rows.
        if output.error and not has_answer:
            # Honour an adapter-declared category (e.g. "quota" for
            # OAuth-limit detection per codeprobe-9xrl). Falls back to
            # the historical "agent" classification when the adapter
            # didn't pin a category.
            error_cat = output.error_category or "agent"
            # Adapter-declared terminal agent failure (codeprobe-8up):
            # a genuine 0.0-reward measurement — status='failed' keeps it
            # on checkpoint resume instead of silently re-running it.
            # Infra casualties (quota, crashes, unknown subtypes) stay
            # status='error' and are retried.
            return TaskResult(
                completed=CompletedTask(
                    task_id=task_id,
                    automated_score=0.0,
                    status="failed" if output.error_terminal else "error",
                    error_category=error_cat,
                    metadata={"error": sanitize_secrets(output.error), **_turn_cap_meta},
                    **_output_fields(),
                ),
                agent_stdout=output.stdout,
                agent_stderr=output.stderr or "",
            )

        try:
            scorer = get_scorer(reward_type)
        except ValueError as exc:
            return TaskResult(
                completed=CompletedTask(
                    task_id=task_id,
                    automated_score=0.0,
                    status="error",
                    metadata={"error": f"Invalid reward_type: {exc}", **_turn_cap_meta},
                    **_output_fields(),
                ),
                agent_stdout=output.stdout,
                agent_stderr=output.stderr or "",
            )

        # Scoring runs in an isolated per-run sandbox; the projection of the
        # ScoreResult into a TaskResult lives in _score_in_sandbox so the
        # scoring stage is a named, independently-testable unit (codeprobe-s6o).
        return _score_in_sandbox(
            task_id=task_id,
            task_dir=task_dir,
            output=output,
            scorer=scorer,
            found_answer=found_answer,
            found_answer_json=found_answer_json,
            dual_mode=dual_mode,
            effective_wt=_effective_wt,
            base_commit=base_commit,
            effective_workspace=effective_workspace,
            turn_cap_meta=_turn_cap_meta,
            output_fields=_output_fields(),
            resolved_preambles=resolved_preambles,
        )
    finally:
        if _owned_dual_iso is not None:
            if _owned_dual_wt is not None:
                try:
                    _owned_dual_iso.release(_owned_dual_wt)
                except Exception:  # pragma: no cover — defensive
                    logger.debug(
                        "[%s] dual-worktree release failed", task_id, exc_info=True
                    )
            try:
                _owned_dual_iso.cleanup()
            except Exception:  # pragma: no cover — defensive
                logger.debug(
                    "[%s] dual-worktree cleanup failed", task_id, exc_info=True
                )


_BILLABLE_COST_MODELS = frozenset({"per_token"})
_BUDGET_WARNING_THRESHOLD = 0.80


def _budget_msg(msg: str) -> None:
    """Print a budget-related message to stderr so it is always visible.

    Uses sys.stderr directly rather than logger.warning() which is
    suppressed at the default INFO log level.
    """
    sys.stderr.write(f"{msg}\n")
    sys.stderr.flush()


def _get_head_ref(repo_path: Path) -> str:
    """Return the current branch name or commit SHA.

    If on a branch, returns the branch name (e.g. ``main``).
    If detached, returns the full commit SHA.
    """
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    # Detached HEAD — return commit SHA
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return "HEAD"


def _git_reset_workdir(
    repo_path: Path,
    *,
    extra_excludes: tuple[str, ...] = (),
    restore_ref: str = "",
) -> None:
    """Reset the working directory to a clean state between sequential tasks.

    Runs ``git restore .`` and ``git clean -fd`` to discard modifications
    and remove untracked files so task N's leftovers don't corrupt task N+1.

    When *restore_ref* is set, also checks out that ref to undo any commit
    pinning from the previous task.

    Also removes ``repo_path/repos/`` if present so multi-repo layouts
    from the previous task don't leak into the next one.
    """
    cleanup_multi_repo_workspace(repo_path)
    try:
        if restore_ref:
            subprocess.run(
                ["git", "checkout", restore_ref],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )
        git_restore_clean(repo_path, extra_excludes=extra_excludes)
    except subprocess.CalledProcessError as exc:
        logger.warning(
            "Git reset failed (exit %d): %s",
            exc.returncode,
            exc.stderr.decode(errors="replace") if exc.stderr else "",
        )
    except OSError as exc:
        logger.warning("Git reset failed: %s", exc)


def _save_task_artifacts(
    runs_dir: Path,
    task_id: str,
    task_result: TaskResult,
) -> None:
    """Save per-task agent output and scoring artifacts.

    Creates runs/{config_label}/{task_id}/ with:
      - agent_output.txt  — raw agent stdout (for trace/debug)
      - agent_error.txt   — raw agent stderr (only if non-empty)
      - mcp_init.json     — offered tool surface (only when captured)
      - scoring.json      — scoring details
    """
    task_dir = runs_dir / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    completed = task_result.completed

    # Agent trace
    if task_result.agent_stdout:
        (task_dir / "agent_output.txt").write_text(
            sanitize_secrets(task_result.agent_stdout), encoding="utf-8"
        )
    if task_result.agent_stderr:
        (task_dir / "agent_error.txt").write_text(
            sanitize_secrets(task_result.agent_stderr), encoding="utf-8"
        )

    # Offered tool surface (codeprobe-9p6) — zero-inference proof of which
    # tools/MCP servers were available this trial. Written whenever the
    # adapter captured a manifest, including the captured-but-empty and
    # failed-attach cases, so an MCP-vs-local comparison can verify the
    # surface per arm instead of inferring it.
    if completed.mcp_init is not None:
        (task_dir / "mcp_init.json").write_text(
            _json.dumps(completed.mcp_init, indent=2) + "\n", encoding="utf-8"
        )

    # Scoring details — emit the unified ScoreResult contract: ``reward``
    # mirrors ``score`` (codeprobe / EB / CSB read either name) and
    # ``diagnostics`` carries run-level cost / time alongside the IR
    # breakdown the scorer already populated. Existing top-level fields
    # (score, status, scorer_family, sub_scores, …) stay so older
    # consumers keep working.
    scoring = {
        "score": completed.automated_score,
        "reward": completed.automated_score,
        "status": completed.status,
        **completed.scoring_details,
    }
    diagnostics: dict = {}
    existing_diag = completed.scoring_details.get("diagnostics")
    if isinstance(existing_diag, dict):
        diagnostics.update(existing_diag)
    diagnostics["task_time_seconds"] = float(completed.duration_seconds)
    if completed.cost_usd is not None:
        diagnostics["token_cost_usd"] = float(completed.cost_usd)
    if completed.input_tokens is not None:
        diagnostics["input_tokens"] = int(completed.input_tokens)
    if completed.cache_read_tokens is not None:
        diagnostics["cache_read_tokens"] = int(completed.cache_read_tokens)
    if completed.cache_creation_tokens is not None:
        diagnostics["cache_creation_tokens"] = int(completed.cache_creation_tokens)
    if completed.output_tokens is not None:
        diagnostics["output_tokens"] = int(completed.output_tokens)
    if completed.num_turns is not None:
        diagnostics["num_turns"] = int(completed.num_turns)
    if completed.result_subtype is not None:
        diagnostics["result_subtype"] = completed.result_subtype
    if completed.duration_api_ms is not None:
        diagnostics["duration_api_ms"] = int(completed.duration_api_ms)
    scoring["diagnostics"] = diagnostics
    (task_dir / "scoring.json").write_text(
        _json.dumps(scoring, indent=2) + "\n", encoding="utf-8"
    )


def _restore_checkpointed(
    checkpoint_store: CheckpointStore | None,
) -> tuple[set[tuple[str, int]], list[CompletedTask]]:
    """Load checkpointed results, returning (id_tuples, results).

    Each id tuple is ``(task_id, repeat_index)`` so that repeat runs
    of the same task are tracked independently.
    """
    if checkpoint_store is None:
        return set(), []
    # Generic over the dataclass fields so a field added to CompletedTask
    # can never be silently dropped on resume (codeprobe-8up; previously
    # ``tool_use_by_name`` was hand-mapped out of existence here). Keys
    # absent from older checkpoint entries fall back to field defaults;
    # unknown keys from newer schemas are ignored.
    field_names = {f.name for f in dataclasses.fields(CompletedTask)}
    ids: set[tuple[str, int]] = set()
    results: list[CompletedTask] = []
    for entry in checkpoint_store.load_entries():
        repeat_index = entry.get("repeat_index", 0)
        ids.add((entry["task_id"], repeat_index))
        results.append(
            CompletedTask(
                **{k: v for k, v in entry.items() if k in field_names}
            )
        )
    return ids, results


def execute_config(
    adapter: AgentAdapter,
    task_dirs: list[Path],
    repo_path: Path,
    experiment_config: ExperimentConfig,
    agent_config: AgentConfig,
    *,
    checkpoint_store: CheckpointStore | None = None,
    runs_dir: Path | None = None,
    on_task_complete: Callable[[CompletedTask], None] | None = None,
    max_cost_usd: float | None = None,
    preamble_resolver: PreambleResolver | None = None,
    parallel: int = 1,
    isolation: IsolationStrategy | None = None,
    repeats: int = 1,
    clean_excludes: tuple[str, ...] = (),
    event_dispatcher: EventDispatcher | None = None,
    trace_recorder: TraceRecorder | None = None,
    config_max_turns_source: str = "",
    pristine_config: bool = False,
) -> list[CompletedTask]:
    """Execute all tasks for a single experiment configuration.

    Resumes from checkpoint if provided. Calls on_task_complete after each task.
    Saves per-task artifacts (agent_output.txt, scoring.json) alongside the
    checkpoint file.

    When *parallel* > 1, tasks are dispatched to a thread pool.  Each agent
    subprocess runs in its own process so threads are IO-bound (waiting for
    the subprocess to finish).

    If *max_cost_usd* is set, the executor accumulates ``cost_usd`` from
    completed tasks whose ``cost_model`` is billable (currently ``per_token``).
    Once cumulative cost exceeds the budget, execution halts and partial
    results are returned.  Tasks with ``unknown`` or ``subscription``
    cost models are skipped in accumulation.

    When *event_dispatcher* is provided, lifecycle events (RunStarted,
    TaskStarted, TaskScored, RunFinished) are emitted.  If *max_cost_usd*
    is also set, a :class:`BudgetChecker` is registered to handle budget
    warnings and halt checks via the event system.

    When *pristine_config* is True, adapters whose ``isolate_session``
    accepts a ``pristine`` kwarg exclude operator personalization
    (CLAUDE.md, settings, skills, ...) from the per-slot config dir so
    arms are reproducible across operators. Both the sequential and
    parallel dispatch paths run the same isolate/cleanup lifecycle.
    """
    checkpointed_ids, results = _restore_checkpointed(checkpoint_store)

    # Filter checkpointed results to only include tasks in the current
    # experiment.  Without this, stale entries from prior runs with different
    # task_ids leak into the results list and inflate/deflate scores.
    current_task_ids = {d.name for d in task_dirs}
    checkpointed_ids = {
        (tid, ri) for tid, ri in checkpointed_ids if tid in current_task_ids
    }
    results = [r for r in results if r.task_id in current_task_ids]

    # Build expanded work items: (task_dir, repeat_index) for all repeats
    all_work: list[tuple[Path, int]] = [
        (d, ri) for d in task_dirs for ri in range(repeats)
    ]
    pending_work = [
        (d, ri) for d, ri in all_work if (d.name, ri) not in checkpointed_ids
    ]
    for d, ri in all_work:
        if (d.name, ri) in checkpointed_ids:
            logger.info("Skipping %s repeat %d (checkpointed)", d.name, ri)

    if not pending_work:
        return results

    # --- Event system setup ---
    budget_checker: BudgetChecker | None = None
    if event_dispatcher is not None and max_cost_usd is not None:
        budget_checker = BudgetChecker(
            budget=max_cost_usd,
            warning_threshold=_BUDGET_WARNING_THRESHOLD,
        )
        budget_checker.set_dispatcher(event_dispatcher)
        event_dispatcher.register(budget_checker)

    if event_dispatcher is not None:
        event_dispatcher.emit(
            RunStarted(
                total_tasks=len(all_work),
                config_label=experiment_config.label,
                timestamp=time.time(),
            )
        )

    cumulative_cost = 0.0

    def _run_one(
        task_dir: Path,
        repeat_index: int = 0,
        worktree_path: Path | None = None,
        session_env: dict[str, str] | None = None,
    ) -> TaskResult:
        logger.info(
            "[%s] Running %s (repeat %d)",
            experiment_config.label,
            task_dir.name,
            repeat_index,
        )
        sem = get_concurrency_semaphore()
        if sem is not None:
            sem.acquire()
        # R5: bind the TraceRecorder to this thread/task so the adapter's
        # JsonStdoutCollector hook writes events for THIS task. Cleared in
        # the finally-block so a later task on the same thread never
        # inherits stale state from an aborted run.
        set_trace = getattr(adapter, "set_trace_context", None)
        wired_trace = False
        if trace_recorder is not None and callable(set_trace):
            set_trace(
                recorder=trace_recorder,
                config=experiment_config.label,
                task_id=task_dir.name,
            )
            wired_trace = True
        try:
            task_result = execute_task(
                adapter=adapter,
                task_dir=task_dir,
                repo_path=repo_path,
                agent_config=agent_config,
                instruction_variant=experiment_config.instruction_variant,
                reward_type=experiment_config.reward_type,
                preamble_names=experiment_config.preambles,
                preamble_resolver=preamble_resolver,
                worktree_path=worktree_path,
                session_env=session_env,
                hide_local_source=experiment_config.hide_local_source,
                config_max_turns_source=config_max_turns_source,
            )
            # Stamp repeat_index on the completed task
            if repeat_index != 0:
                from dataclasses import replace

                task_result = TaskResult(
                    completed=replace(task_result.completed, repeat_index=repeat_index),
                    agent_stdout=task_result.agent_stdout,
                    agent_stderr=task_result.agent_stderr,
                )
            return task_result
        finally:
            if wired_trace and callable(set_trace):
                set_trace(recorder=None, config=None, task_id=None)
            if sem is not None:
                sem.release()

    def _crash_result(
        task_dir: Path, repeat_index: int, exc: BaseException
    ) -> TaskResult:
        """Preserve a per-task crash as one ``status="error"`` result.

        Shared by both the sequential and parallel dispatch paths so an
        uncaught exception (a scorer bug, a KeyError, …) is scored as a
        single error trial instead of being dropped or aborting the whole
        config (codeprobe-s6o). CLAUDE.md: don't drop score failures.
        """
        logger.error(
            "[%s] %s repeat %d raised: %s",
            experiment_config.label,
            task_dir.name,
            repeat_index,
            exc,
        )
        return TaskResult(
            completed=CompletedTask(
                task_id=task_dir.name,
                automated_score=0.0,
                repeat_index=repeat_index,
                status="error",
                error_category=_classify_error(exc),
                # Crash before the per-trial cap was resolved — keep the
                # envelope schema uniform with an unresolved cap.
                metadata={
                    "error": str(exc),
                    "max_turns_chosen": None,
                    "max_turns_source": "",
                },
            ),
        )

    budget_warning_emitted = False
    # Set when an adapter signals an unrecoverable error category (e.g.
    # OAuth quota exhausted) — the dispatch loop checks this and halts
    # rather than wasting wall-clock on guaranteed-failing trials
    # (codeprobe-9xrl).
    quota_exhausted = False
    quota_message: str | None = None

    def _handle_result(task_result: TaskResult) -> None:
        nonlocal cumulative_cost, budget_warning_emitted
        nonlocal quota_exhausted, quota_message
        result = task_result.completed
        results.append(result)

        # Detect quota exhaustion from adapter-declared error category.
        # First detection wins — subsequent ones don't change behaviour
        # but their messages are preserved on the individual results.
        if result.error_category == "quota" and not quota_exhausted:
            quota_exhausted = True
            quota_message = (
                result.metadata.get("error") if result.metadata else None
            )
            _budget_msg(
                f"OAuth quota exhausted — halting run after current "
                f"in-flight tasks complete. Remaining trials will be "
                f"cancelled. Message: {quota_message or '(no detail)'}"
            )

        if runs_dir is not None:
            artifact_id = result.task_id
            if result.repeat_index > 0:
                artifact_id = f"{result.task_id}/repeat-{result.repeat_index}"
            _save_task_artifacts(runs_dir, artifact_id, task_result)

        if checkpoint_store is not None:
            checkpoint_store.append(result)

        if on_task_complete is not None:
            on_task_complete(result)

        # Emit TaskScored event when dispatcher is available
        if event_dispatcher is not None:
            event_dispatcher.emit(
                TaskScored(
                    task_id=result.task_id,
                    config_label=experiment_config.label,
                    automated_score=result.automated_score,
                    duration_seconds=result.duration_seconds,
                    cost_usd=result.cost_usd,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    cache_read_tokens=result.cache_read_tokens,
                    cache_creation_tokens=result.cache_creation_tokens,
                    cost_model=result.cost_model,
                    cost_source=result.cost_source,
                    error=result.metadata.get("error") if result.metadata else None,
                    timestamp=time.time(),
                    scoring_details=dict(result.scoring_details),
                )
            )

        if result.cost_model in _BILLABLE_COST_MODELS and result.cost_usd is not None:
            cumulative_cost += result.cost_usd

        # Emit 80% budget warning once (legacy path — no dispatcher)
        if (
            event_dispatcher is None
            and max_cost_usd is not None
            and not budget_warning_emitted
            and cumulative_cost >= max_cost_usd * _BUDGET_WARNING_THRESHOLD
            and cumulative_cost <= max_cost_usd
        ):
            budget_warning_emitted = True
            pct = int(cumulative_cost / max_cost_usd * 100)
            _budget_msg(
                f"Cost warning: ${cumulative_cost:.2f} of "
                f"${max_cost_usd:.2f} budget used ({pct}%)"
            )

    workers = min(parallel, len(pending_work))

    def _budget_exceeded() -> bool:
        """Check whether the cost budget has been exceeded."""
        if budget_checker is not None:
            return budget_checker.is_exceeded
        return max_cost_usd is not None and cumulative_cost > max_cost_usd

    def _should_halt() -> bool:
        """Stop dispatching new trials when budget is exhausted OR a
        quota error has been detected (codeprobe-9xrl).
        """
        return _budget_exceeded() or quota_exhausted

    # Capture original HEAD so we can restore it after commit pinning.
    original_ref = _get_head_ref(repo_path)

    # Quarantine sibling experiment dirs at the repo root for the duration of
    # the dispatch.  Without this, an agent in a slot worktree can ``cd ../..``
    # to the repo root and read another experiment's ground_truth.json.
    active_exp_dir = _find_active_experiment_dir(task_dirs, repo_path)
    quarantine_cm: contextlib.AbstractContextManager[None] = (
        quarantine_sibling_experiments(repo_path, active_exp_dir)
        if active_exp_dir is not None
        else contextlib.nullcontext()
    )

    with quarantine_cm:
        if workers <= 1:
            # Sequential — preserves original behavior and budget checks.
            # Session isolation mirrors the parallel branch: one namespaced
            # slot env for the whole config, cleaned up in the finally.
            # Without it, serial runs used the real ~/.claude wholesale and
            # serial vs parallel arms saw different config state.
            session_namespace = _build_session_namespace(experiment_config.label)
            try:
                sess_env = _call_isolate_session(
                    adapter,
                    0,
                    namespace=session_namespace,
                    pristine=pristine_config,
                )
                for idx, (task_dir, repeat_index) in enumerate(pending_work):
                    if quota_exhausted:
                        _budget_msg(
                            "OAuth quota exhausted — halting remaining "
                            f"{len(pending_work) - idx} trials"
                        )
                        break
                    if _budget_exceeded():
                        _budget_msg(
                            f"Cost budget exceeded: ${cumulative_cost:.2f} > "
                            f"${max_cost_usd:.2f} — halting"
                        )
                        break
                    # Emit TaskStarted event
                    if event_dispatcher is not None:
                        event_dispatcher.emit(
                            TaskStarted(
                                task_id=task_dir.name,
                                config_label=experiment_config.label,
                                timestamp=time.time(),
                            )
                        )
                    # Reset working directory between tasks so leftovers from
                    # task N don't corrupt task N+1's results.  Also restores
                    # the original branch/HEAD in case the previous task pinned
                    # to a specific commit.
                    if idx > 0:
                        _git_reset_workdir(
                            repo_path,
                            extra_excludes=clean_excludes,
                            restore_ref=original_ref,
                        )
                    try:
                        task_result = _run_one(
                            task_dir,
                            repeat_index=repeat_index,
                            session_env=sess_env,
                        )
                    except Exception as exc:  # noqa: BLE001 — preserve, don't drop
                        # Mirror the parallel path: a per-task crash becomes one
                        # error result and the rest of the config still runs
                        # (codeprobe-s6o). Without this, an uncaught scorer
                        # exception aborted execute_config and dropped every
                        # already-collected result for this config.
                        task_result = _crash_result(task_dir, repeat_index, exc)
                    _handle_result(task_result)
                # Restore original HEAD after all sequential tasks complete so
                # the repo isn't left on a detached commit from the last task.
                _git_reset_workdir(
                    repo_path, extra_excludes=clean_excludes, restore_ref=original_ref
                )
            finally:
                _cleanup_session_namespace(adapter, session_namespace)
        else:
            # Parallel — dispatch all pending tasks to thread pool
            logger.info(
                "[%s] Dispatching %d work items with %d workers",
                experiment_config.label,
                len(pending_work),
                workers,
            )
            session_namespace = _build_session_namespace(experiment_config.label)
            # Auto-create isolation when parallel > 1 and none provided
            owns_isolation = False
            active_isolation = isolation
            if active_isolation is None:
                active_isolation = WorktreeIsolation(
                    repo_path, pool_size=workers, namespace=experiment_config.label
                )
                owns_isolation = True

            def _run_isolated(task_dir: Path, repeat_index: int) -> TaskResult:
                # Emit TaskStarted event
                if event_dispatcher is not None:
                    event_dispatcher.emit(
                        TaskStarted(
                            task_id=task_dir.name,
                            config_label=experiment_config.label,
                            timestamp=time.time(),
                        )
                    )
                wt = active_isolation.acquire()
                try:
                    # Extract slot index from worktree path name (e.g. "slot-0" → 0)
                    slot_name = wt.name
                    try:
                        slot_id = int(slot_name.rsplit("-", 1)[-1])
                    except (ValueError, IndexError):
                        slot_id = 0
                    sess_env = _call_isolate_session(
                        adapter,
                        slot_id,
                        namespace=session_namespace,
                        pristine=pristine_config,
                    )
                    return _run_one(
                        task_dir,
                        repeat_index=repeat_index,
                        worktree_path=wt,
                        session_env=sess_env,
                    )
                finally:
                    active_isolation.release(wt)

            try:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    future_to_work = {
                        pool.submit(_run_isolated, td, ri): (td, ri)
                        for td, ri in pending_work
                    }
                    for future in as_completed(future_to_work):
                        task_dir, repeat_index = future_to_work[future]
                        try:
                            task_result = future.result()
                        except Exception as exc:  # noqa: BLE001 — preserve, don't drop
                            task_result = _crash_result(task_dir, repeat_index, exc)
                        _handle_result(task_result)

                        # Halt on either budget exhaustion or quota
                        # detection (codeprobe-9xrl). Both are
                        # unrecoverable within this run; only
                        # not-yet-started futures are cancellable, but
                        # that's still cheaper than letting them all
                        # run to a guaranteed failure.
                        if _should_halt():
                            if quota_exhausted:
                                _budget_msg(
                                    "OAuth quota exhausted — cancelling "
                                    "pending trials"
                                )
                            else:
                                _budget_msg(
                                    f"Cost budget exceeded: ${cumulative_cost:.2f} > "
                                    f"${max_cost_usd:.2f} — halting"
                                )
                            for f in future_to_work:
                                f.cancel()
                            break
            finally:
                _cleanup_session_namespace(adapter, session_namespace)
                if owns_isolation:
                    active_isolation.cleanup()

    # Warn if >30% of tasks have system errors (capacity issues)
    if results:
        system_errors = sum(1 for r in results if r.error_category == "system")
        ratio = system_errors / len(results)
        if ratio > 0.30:
            logger.warning(
                "[%s] %.0f%% of tasks (%d/%d) have system errors — "
                "possible capacity issues",
                experiment_config.label,
                ratio * 100,
                system_errors,
                len(results),
            )

    # Emit RunFinished event with summary stats
    if event_dispatcher is not None:
        completed_count = len(results)
        scores = [r.automated_score for r in results]
        mean_score = sum(scores) / len(scores) if scores else 0.0
        total_cost = sum(
            r.cost_usd
            for r in results
            if r.cost_usd is not None and r.cost_model in _BILLABLE_COST_MODELS
        )
        total_duration = sum(r.duration_seconds for r in results)
        event_dispatcher.emit(
            RunFinished(
                total_tasks=len(all_work),
                completed_count=completed_count,
                mean_score=mean_score,
                total_cost=total_cost,
                total_duration=total_duration,
                config_label=experiment_config.label,
                timestamp=time.time(),
            )
        )

    return results
