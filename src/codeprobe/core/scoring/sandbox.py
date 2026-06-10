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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codeprobe.core.scoring.materialize import AgentState

logger = logging.getLogger(__name__)

_TOKEN_PATTERN = re.compile(
    r"("
    r"ghp_[A-Za-z0-9]{36}"  # GitHub personal access token
    r"|gho_[A-Za-z0-9]{36}"  # GitHub OAuth token
    r"|github_pat_[A-Za-z0-9_]{80,}"  # GitHub fine-grained PAT
    r"|sk-[A-Za-z0-9]{32,}"  # OpenAI / Anthropic API key
    r"|sk-ant-[A-Za-z0-9\-]{80,}"  # Anthropic API key (long form)
    r"|AKIA[0-9A-Z]{16}"  # AWS access key ID
    r"|Bearer\s+\S{20,}"  # Authorization bearer tokens
    r"|token\s+\S{20,}"  # Generic token patterns
    r")",
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
    """Result of running a script inside the sandbox."""

    returncode: int
    stdout: str
    stderr: str
    sandbox_dir: Path | None = None
    error: str | None = None
    materialized_via: str = "in_place"
    verifier_error: bool = False

    @property
    def sandbox_task(self) -> Path | None:
        return self.sandbox_dir / "task" if self.sandbox_dir else None


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

        env = _safe_env(env_extra)

        result = subprocess.run(
            ["bash", str(sandbox_script)],
            env=env,
            cwd=str(sandbox_task),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if cleanup:
            shutil.rmtree(sandbox_dir, ignore_errors=True)
            sandbox_dir = None
        return _SandboxRun(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            sandbox_dir=sandbox_dir,
            materialized_via=materialized_via,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        if sandbox_dir is not None:
            shutil.rmtree(sandbox_dir, ignore_errors=True)
        if isinstance(exc, subprocess.TimeoutExpired):
            error = "Scoring timed out"
        else:
            error = str(exc)
            logger.warning("Sandbox setup failed (OSError): %s", error)
        return _SandboxRun(returncode=-1, stdout="", stderr="", error=error)
