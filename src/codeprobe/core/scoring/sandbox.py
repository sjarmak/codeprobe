"""Sandbox execution primitives — secret redaction, filtered environment,
thread-local env overrides, and the sandboxed script runner.

Split out of the original ``codeprobe/core/scoring.py`` module (pure
mechanical move — see the package ``__init__`` for the public surface).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from codeprobe.sandbox import runner as container_runner

if TYPE_CHECKING:
    from codeprobe.core.scoring.materialize import AgentState

from codeprobe.config.redact import token_freetext_pattern

logger = logging.getLogger(__name__)

# Prefix-derived token shapes come from the canonical list in
# config/redact.py (single source of truth); only non-prefix shapes
# are enumerated here.
_TOKEN_PATTERN = re.compile(
    r"("
    + token_freetext_pattern().pattern
    + r"|github_pat_[A-Za-z0-9_]{80,}"  # GitHub fine-grained PAT
    + r"|AKIA[0-9A-Z]{16}"  # AWS access key ID
    + r"|Bearer\s+\S{20,}"  # Authorization bearer tokens
    + r"|token\s+\S{20,}"  # Generic token patterns
    + r")",
    re.IGNORECASE,
)

SCORE_TIMEOUT_SECONDS = 300

# Patterns excluded from sandbox copytree to keep per-task IO bounded.
# Any future task format that legitimately needs one of these paths
# should override this at the writer level, not suppress it here.
COPYTREE_IGNORE = (
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "target",
    "build",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
)


# ---------------------------------------------------------------------------
# Shared sandbox helpers
# ---------------------------------------------------------------------------


def sanitize_secrets(text: str) -> str:
    """Redact potential secrets (API keys, tokens) from text."""
    return _TOKEN_PATTERN.sub("[REDACTED]", text)


_SAFE_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "TERM",
        "TMPDIR",
        "LC_ALL",
        # Go toolchain
        "GOPATH",
        "GOROOT",
        "GOMODCACHE",
        "GOCACHE",
        "GOFLAGS",
        # Rust toolchain
        "CARGO_HOME",
        "RUSTUP_HOME",
        # Node/npm
        "NODE_PATH",
        "NPM_CONFIG_PREFIX",
        # Python
        "VIRTUAL_ENV",
        "PYTHONPATH",
    }
)


# Thread-local env overrides for sandboxed scorer subprocesses. Callers use
# :func:`scorer_env_override` as a context manager to bind extra env vars
# (e.g. ``TASK_REPO_ROOT`` for dual tasks) so test.sh can cd into a
# per-run worktree instead of the shared mined repo_path. Raw threads
# each get their own override — no cross-thread leakage.
_scorer_env_tls = threading.local()


def _thread_env_overrides() -> dict[str, str]:
    return getattr(_scorer_env_tls, "overrides", None) or {}


@contextmanager
def scorer_env_override(overrides: dict[str, str] | None) -> Iterator[None]:
    """Bind a thread-local env overlay visible to sandboxed scorer processes.

    ``overrides`` is merged into the filtered env built by :func:`_safe_env`.
    The previous overlay is restored on exit, so nested overrides compose
    in LIFO order.
    """
    previous = _thread_env_overrides()
    _scorer_env_tls.overrides = dict(overrides) if overrides else {}
    try:
        yield
    finally:
        _scorer_env_tls.overrides = previous


def _safe_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a filtered environment with only safe keys.

    Prevents secret leakage via inherited environment variables. Any
    thread-local overrides bound via :func:`scorer_env_override` are merged
    on top of the filtered env, and the caller's ``extra`` takes highest
    precedence.
    """
    env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}
    env.update(_thread_env_overrides())
    if extra:
        env.update(extra)
    return env


@dataclass(frozen=True)
class _SandboxRun:
    """Result of running a script inside the sandbox.

    ``execution_mode`` records HOW the script process ran: ``"container"``
    (docker/podman with ``--network=none`` via
    :func:`codeprobe.sandbox.runner.run_in_sandbox`), ``"host"`` (plain
    ``bash`` subprocess on the invoking machine), or ``"none"`` (refused
    or failed before any script executed — nothing ran anywhere). Scorers
    surface it as ``scoring_details["sandbox_execution"]`` so every
    summary can disclose the containment level of the verifier run. The
    field is required (keyword-only, no default) so no construction site
    can silently claim host execution for a trial that never executed.
    """

    returncode: int
    stdout: str
    stderr: str
    sandbox_dir: Path | None = None
    error: str | None = None
    materialized_via: str = "in_place"
    verifier_error: bool = False
    execution_mode: str = field(kw_only=True)

    @property
    def sandbox_task(self) -> Path | None:
        return self.sandbox_dir / "task" if self.sandbox_dir else None


@dataclass(frozen=True)
class _PreparedSandbox:
    sandbox_dir: Path
    sandbox_task: Path
    sandbox_script: Path
    env_extra: dict[str, str]


def _configured_scoring_image() -> tuple[str | None, str | None]:
    try:
        return container_runner.scoring_image_reference(), None
    except ValueError as exc:
        return None, str(exc)


def _missing_image_refusal(
    engine: str | None, scoring_image: str | None, image_error: str | None
) -> str | None:
    """Refusal message when host fallback is not consented, else ``None``.

    Called only when the container path is unavailable because the scoring
    image is missing. Host execution stays allowed for a ``host-consented``
    plan (the user passed ``--uncontained``) and for plan-less library/test
    callers (programmatic use never set a plan — preserve their behavior).
    An engine-less machine also falls through: a ``sandboxed`` plan there
    means the environment itself is the containment (e.g. we are already
    inside a container), so the bash path is the contained path.
    """
    if engine is None:
        return None
    from codeprobe.core.containment import active_plan

    plan = active_plan()
    if plan is None or plan.mode == "host-consented":
        return None
    if scoring_image is None:
        return (
            "Container engine found but scoring image is not configured; "
            f"{image_error or 'set an exact image reference'}. "
            f"{container_runner.image_configuration_remediation()}"
        )
    return (
        "Container engine found but scoring image "
        f"{scoring_image!r} is not available locally; refusing "
        "to run mined test/verifier scripts on the host without "
        "--uncontained consent. Pull and verify the configured trusted "
        "images with: codeprobe bootstrap"
    )


def _container_exec(
    sandbox_script: Path,
    sandbox_dir: Path,
    sandbox_task: Path,
    env_extra: dict[str, str],
    timeout: int,
    scoring_image: str,
) -> tuple[int, str, str] | str:
    """Run *sandbox_script* in the scoring container.

    Returns ``(returncode, stdout, stderr)`` on completion, or an error
    string when the engine itself failed (timeout, launch error, denied
    write). The identity mount (host path == container path) keeps the
    ``AGENT_OUTPUT`` / ``TASK_REPO_ROOT`` host paths in ``env_extra`` valid
    inside the container with no translation. ``allow_writes=True`` is
    required because scripts write reward.txt/answer artefacts into the
    sandbox copy — the copy is a throwaway tempdir, so rw is safe. Only
    ``env_extra`` is forwarded (not the full ``_safe_env``): the image
    supplies its own PATH/HOME. Network stays ``--network=none``; mined
    tests that need egress fail closed, which is the intended posture.
    """
    try:
        result = container_runner.run_in_sandbox(
            ["bash", str(sandbox_script)],
            {str(sandbox_dir): str(sandbox_dir)},
            allow_writes=True,
            image=scoring_image,
            timeout=float(timeout),
            workdir=str(sandbox_task),
            env=env_extra,
        )
    except container_runner.SandboxError as exc:
        # Covers SandboxWriteDeniedError (subclass) too. Mirror the host
        # path's TimeoutExpired handling: error populated, no crash.
        return str(exc)
    return (result.exit_code, result.stdout, result.stderr)


def _prepare_sandbox(
    script_path: Path, agent_output: str, task_dir: Path
) -> _PreparedSandbox:
    sandbox_dir = Path(tempfile.mkdtemp(prefix="codeprobe-score-"))
    try:
        sandbox_task = sandbox_dir / "task"
        shutil.copytree(
            task_dir,
            sandbox_task,
            symlinks=False,
            ignore=shutil.ignore_patterns(*COPYTREE_IGNORE),
        )
        sandbox_script = sandbox_task / script_path.relative_to(task_dir)
        output_file = sandbox_dir / "agent_output.txt"
        output_file.write_text(agent_output, encoding="utf-8")
        return _PreparedSandbox(
            sandbox_dir=sandbox_dir,
            sandbox_task=sandbox_task,
            sandbox_script=sandbox_script,
            env_extra={"AGENT_OUTPUT": str(output_file)},
        )
    except OSError:
        _cleanup_dir(sandbox_dir)
        raise


def _maybe_materialize_workspace(
    prepared: _PreparedSandbox, agent_state: AgentState | None
) -> tuple[_PreparedSandbox, str, str | None]:
    from codeprobe.core import scoring as _scoring_pkg

    ws_state = agent_state
    if not (
        ws_state is not None
        and ws_state.base_commit
        and _scoring_pkg._is_git_repo(ws_state.workspace)
    ):
        return prepared, "in_place", None
    checkout, err = _scoring_pkg._materialize_workspace(
        ws_state, prepared.sandbox_dir
    )
    if err is not None:
        return prepared, "git_apply", err
    if checkout is None:
        raise RuntimeError("_materialize_workspace returned invalid empty result")
    env_extra = {**prepared.env_extra, "TASK_REPO_ROOT": str(checkout)}
    return replace(prepared, env_extra=env_extra), "git_apply", None


def _cleanup_dir(path: Path | None) -> None:
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)


def _error_run(
    *,
    error: str,
    stderr: str,
    sandbox_dir: Path | None,
    materialized_via: str,
    execution_mode: str,
    verifier_error: bool = False,
) -> _SandboxRun:
    return _SandboxRun(
        returncode=-1,
        stdout="",
        stderr=stderr,
        sandbox_dir=sandbox_dir,
        error=error,
        materialized_via=materialized_via,
        verifier_error=verifier_error,
        execution_mode=execution_mode,
    )


def _finish_error_run(
    prepared: _PreparedSandbox,
    error: str,
    materialized_via: str,
    execution_mode: str,
    cleanup: bool,
    *,
    stderr: str | None = None,
    verifier_error: bool = False,
) -> _SandboxRun:
    sandbox_dir = None if cleanup else prepared.sandbox_dir
    if cleanup:
        _cleanup_dir(prepared.sandbox_dir)
    return _error_run(
        error=error,
        stderr=error if stderr is None else stderr,
        sandbox_dir=sandbox_dir,
        materialized_via=materialized_via,
        execution_mode=execution_mode,
        verifier_error=verifier_error,
    )


def _container_outcome(
    prepared: _PreparedSandbox, timeout: int
) -> tuple[tuple[int, str, str] | str | None, str | None]:
    engine = container_runner.detect_engine()
    plan_refusal = _container_plan_refusal(engine)
    if plan_refusal is not None:
        return None, plan_refusal
    scoring_image, image_error = _configured_scoring_image()
    if engine is not None and scoring_image is not None:
        if container_runner.image_available(engine, scoring_image):
            outcome = _container_exec(
                prepared.sandbox_script,
                prepared.sandbox_dir,
                prepared.sandbox_task,
                prepared.env_extra,
                timeout,
                scoring_image,
            )
            return outcome, None
    return None, _missing_image_refusal(engine, scoring_image, image_error)


def _container_plan_refusal(engine: str | None) -> str | None:
    from codeprobe.core.containment import active_plan

    plan = active_plan()
    if plan is None or plan.mode != "container":
        return None
    if engine == plan.engine and engine is not None:
        return None
    return (
        "The authorized container engine is no longer available; refusing "
        "to run mined test/verifier scripts on the host without "
        "--uncontained consent."
    )


def _host_exec(prepared: _PreparedSandbox, timeout: int) -> tuple[int, str, str]:
    result = subprocess.run(
        ["bash", str(prepared.sandbox_script)],
        env=_safe_env(prepared.env_extra),
        cwd=str(prepared.sandbox_task),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.returncode, result.stdout, result.stderr


def _execute_prepared_sandbox(
    prepared: _PreparedSandbox, timeout: int, materialized_via: str, cleanup: bool
) -> _SandboxRun:
    outcome, refusal = _container_outcome(prepared, timeout)
    if refusal is not None:
        return _finish_error_run(
            prepared, refusal, materialized_via, "none", cleanup,
            verifier_error=True
        )
    if outcome is not None:
        if isinstance(outcome, str):
            return _finish_error_run(
                prepared, outcome, materialized_via, "container", cleanup,
                stderr=""
            )
        return _finish_success_run(
            prepared, outcome, materialized_via, "container", cleanup
        )
    return _finish_success_run(
        prepared, _host_exec(prepared, timeout), materialized_via, "host", cleanup
    )


def _finish_success_run(
    prepared: _PreparedSandbox,
    outcome: tuple[int, str, str],
    materialized_via: str,
    execution_mode: str,
    cleanup: bool,
) -> _SandboxRun:
    returncode, stdout, stderr = outcome
    sandbox_dir = None if cleanup else prepared.sandbox_dir
    if cleanup:
        _cleanup_dir(prepared.sandbox_dir)
    return _SandboxRun(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        sandbox_dir=sandbox_dir,
        materialized_via=materialized_via,
        execution_mode=execution_mode,
    )


def _run_in_sandbox(
    script_path: Path,
    agent_output: str,
    task_dir: Path,
    *,
    timeout: int | None = None,
    cleanup: bool = True,
    agent_state: AgentState | None = None,
) -> _SandboxRun:
    """Execute *script_path* inside a sandboxed copy of *task_dir*."""
    from codeprobe.core import scoring as _scoring_pkg

    if timeout is None:
        timeout = _scoring_pkg.SCORE_TIMEOUT_SECONDS
    sandbox_dir = None
    try:
        prepared = _prepare_sandbox(script_path, agent_output, task_dir)
        sandbox_dir = prepared.sandbox_dir
        prepared, materialized_via, error = _maybe_materialize_workspace(
            prepared, agent_state
        )
        if error is not None:
            return _finish_error_run(
                prepared, error, materialized_via, "none", cleanup,
                verifier_error=True
            )
        return _execute_prepared_sandbox(prepared, timeout, materialized_via, cleanup)
    except (subprocess.TimeoutExpired, OSError) as exc:
        _cleanup_dir(sandbox_dir)
        if isinstance(exc, subprocess.TimeoutExpired):
            error = "Scoring timed out"
            execution_mode = "host"
        else:
            error = "Sandbox setup failed"
            execution_mode = "none"
            logger.warning("Sandbox setup failed (OSError)")
        return _error_run(
            error=error,
            stderr="",
            sandbox_dir=None,
            materialized_via="in_place",
            execution_mode=execution_mode,
        )
