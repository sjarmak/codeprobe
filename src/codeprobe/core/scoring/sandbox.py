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
from dataclasses import dataclass, field
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


def _missing_image_refusal(engine: str | None) -> str | None:
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
    scoring_image = container_runner.scoring_image_reference()
    build_tag = container_runner.scoring_image_build_tag()
    return (
        "Container engine found but scoring image "
        f"{scoring_image!r} is not available locally; refusing "
        "to run mined test/verifier scripts on the host without "
        "--uncontained consent. Pull a trusted published digest, or build "
        "the image from the repo root with: "
        "docker build -f src/codeprobe/sandbox/Dockerfile.scoring "
        f"-t {build_tag} . Override the reference with "
        f"{container_runner.SCORING_IMAGE_ENV} when using a private mirror."
    )


def _container_exec(
    sandbox_script: Path,
    sandbox_dir: Path,
    sandbox_task: Path,
    env_extra: dict[str, str],
    timeout: int,
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
        scoring_image = container_runner.scoring_image_reference()
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


def _run_in_sandbox(
    script_path: Path,
    agent_output: str,
    task_dir: Path,
    *,
    timeout: int | None = None,
    cleanup: bool = True,
    agent_state: AgentState | None = None,
) -> _SandboxRun:
    """Execute *script_path* inside a sandboxed copy of *task_dir*.

    Returns a _SandboxRun with process results and paths into the sandbox
    so callers can inspect files written by the script.  When *cleanup* is
    True the sandbox is removed before returning; set to False when the
    caller needs to read sandbox artefacts (caller must clean up).

    When *agent_state* is set AND its workspace is a git repo, the
    sandbox additionally materialises a clean checkout at
    ``agent_state.base_commit`` with the agent's full diff applied;
    ``TASK_REPO_ROOT`` is overridden to that checkout so the verifier
    sees an isolated tree rather than the agent's dirty worktree.
    Failures in the materialisation route to a ``_SandboxRun`` with
    ``verifier_error=True`` — the script is NOT run.
    """
    # Resolve collaborators through the package namespace at call time:
    # the pre-package module resolved these names via module globals, and
    # tests rebind ``codeprobe.core.scoring.SCORE_TIMEOUT_SECONDS`` on the
    # package. Importing ``materialize`` at module top would also be
    # circular (it imports ``sanitize_secrets`` from this module).
    from codeprobe.core import scoring as _scoring_pkg

    if timeout is None:
        timeout = _scoring_pkg.SCORE_TIMEOUT_SECONDS
    sandbox_dir = None
    try:
        sandbox_dir = Path(tempfile.mkdtemp(prefix="codeprobe-score-"))
        sandbox_task = sandbox_dir / "task"
        shutil.copytree(
            task_dir,
            sandbox_task,
            symlinks=False,
            ignore=shutil.ignore_patterns(*COPYTREE_IGNORE),
        )

        rel = script_path.relative_to(task_dir)
        sandbox_script = sandbox_task / rel

        output_file = sandbox_dir / "agent_output.txt"
        output_file.write_text(agent_output, encoding="utf-8")

        materialized_via = "in_place"
        env_extra: dict[str, str] = {"AGENT_OUTPUT": str(output_file)}

        # Bind ``agent_state`` to a local so the narrowing carries through
        # without a mypy ``# for mypy`` assert. The double-condition reads
        # cleanly: passed + non-empty commit + workspace is a git repo.
        ws_state = agent_state
        if (
            ws_state is not None
            and ws_state.base_commit
            and _scoring_pkg._is_git_repo(ws_state.workspace)
        ):
            checkout, err = _scoring_pkg._materialize_workspace(
                ws_state, sandbox_dir
            )
            if err is not None:
                # apply_check failure / clone failure / diff capture failure
                # — surface as verifier_error and skip script execution.
                if cleanup:
                    shutil.rmtree(sandbox_dir, ignore_errors=True)
                    sandbox_dir = None
                return _SandboxRun(
                    returncode=-1,
                    stdout="",
                    stderr=err,
                    sandbox_dir=sandbox_dir,
                    error=err,
                    materialized_via="git_apply",
                    verifier_error=True,
                    execution_mode="none",
                )
            # The (checkout, err) contract of ``_materialize_workspace``
            # is: exactly one of them is set. Defensive raise here
            # surfaces a contract bug loudly rather than NoneType-
            # crashing inside the test.sh subprocess.
            if checkout is None:
                raise RuntimeError(
                    "_materialize_workspace returned (None, None) — "
                    "contract violation"
                )
            materialized_via = "git_apply"
            # test.sh that hardcodes ``cd $TASK_REPO_ROOT`` (mined dual
            # tasks do) now lands inside the materialised checkout.
            env_extra["TASK_REPO_ROOT"] = str(checkout)

        # Containment branch (codeprobe-f7rl.4): mined scripts are untrusted
        # third-party code. When a container engine and the scoring image
        # are both available, execute inside the --network=none container;
        # otherwise host execution needs consent (see _missing_image_refusal).
        engine = container_runner.detect_engine()
        scoring_image = container_runner.scoring_image_reference()
        if engine is not None and container_runner.image_available(engine, scoring_image):
            execution_mode = "container"
            outcome = _container_exec(
                sandbox_script, sandbox_dir, sandbox_task, env_extra, timeout
            )
            if isinstance(outcome, str):
                if cleanup:
                    shutil.rmtree(sandbox_dir, ignore_errors=True)
                    sandbox_dir = None
                return _SandboxRun(
                    returncode=-1,
                    stdout="",
                    stderr="",
                    sandbox_dir=sandbox_dir,
                    error=outcome,
                    materialized_via=materialized_via,
                    execution_mode="container",
                )
            returncode, stdout, stderr = outcome
        else:
            refusal = _missing_image_refusal(engine)
            if refusal is not None:
                if cleanup:
                    shutil.rmtree(sandbox_dir, ignore_errors=True)
                    sandbox_dir = None
                return _SandboxRun(
                    returncode=-1,
                    stdout="",
                    stderr=refusal,
                    sandbox_dir=sandbox_dir,
                    error=refusal,
                    materialized_via=materialized_via,
                    verifier_error=True,
                    execution_mode="none",
                )
            execution_mode = "host"
            result = subprocess.run(
                ["bash", str(sandbox_script)],
                env=_safe_env(env_extra),
                cwd=str(sandbox_task),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            returncode, stdout, stderr = (
                result.returncode,
                result.stdout,
                result.stderr,
            )
        if cleanup:
            shutil.rmtree(sandbox_dir, ignore_errors=True)
            sandbox_dir = None
        return _SandboxRun(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            sandbox_dir=sandbox_dir,
            materialized_via=materialized_via,
            execution_mode=execution_mode,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        if sandbox_dir is not None:
            shutil.rmtree(sandbox_dir, ignore_errors=True)
        if isinstance(exc, subprocess.TimeoutExpired):
            # Only the host bash path raises TimeoutExpired here — the
            # container path translates timeouts into SandboxError inside
            # _container_exec (and force-removes the container).
            error = "Scoring timed out"
            execution_mode = "host"
        else:
            # OSError is sandbox setup (mkdtemp/copytree/write) or a
            # failed exec — nothing ran.
            error = str(exc)
            execution_mode = "none"
            logger.warning("Sandbox setup failed (OSError): %s", error)
        return _SandboxRun(
            returncode=-1,
            stdout="",
            stderr="",
            error=error,
            execution_mode=execution_mode,
        )
