"""Diff-materialisation pipeline (Slice 1b) — capture the agent's workspace
diff, create a fresh checkout at the captured base commit, and apply the
diff so the verifier sees a clean tree. Also home of :class:`AgentState`.

Split out of the original ``codeprobe/core/scoring.py`` module (pure
mechanical move — see the package ``__init__`` for the public surface).
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from codeprobe.core.scoring.sandbox import sanitize_secrets


@dataclass(frozen=True)
class AgentState:
    """Captured agent-workspace state for the diff-materialisation path.

    The executor stashes ``base_commit = git rev-parse HEAD`` of the
    agent's workspace BEFORE ``adapter.run`` so the scorer can later
    materialise a clean checkout at that SHA and replay the agent's
    full diff on top. Slice 1b (bead codeprobe-xysn).

    ``base_commit`` is the workspace HEAD SHA at executor-time.
    ``workspace`` is the agent's effective workspace directory (the
    per-trial worktree in dual mode, or ``repo_path`` for single-tenant
    runs). The scorer treats this as opaque — it only reads ``.git/``
    and runs ``git`` commands against it.

    When ``workspace`` is not a git repo (or ``base_commit`` is empty),
    the scorer falls back to the legacy ``in_place`` path.
    """

    base_commit: str
    workspace: Path



# ---------------------------------------------------------------------------
# Diff materialisation helpers (Slice 1b — codeprobe-xysn)
# ---------------------------------------------------------------------------
#
# These run a strict three-step pipeline:
#
#   1. _capture_workspace_diff  → stage everything and capture the unified
#      diff (committed + staged + unstaged + untracked) relative to the
#      executor's captured base_commit.
#   2. _create_fresh_checkout   → clone the workspace (local, no hardlinks)
#      into a sandbox-owned tempdir, then detach to base_commit so the
#      clone tree is exactly what the agent started from.
#   3. _apply_diff              → git apply --check first (fail-loud on
#      conflict / binary corruption / missing renames), then git apply
#      --binary to land the changes.
#
# ``materialize_workspace`` orchestrates them. On any failure the path
# routes to ``verifier_error`` and the verifier is NOT run.


_GIT_TIMEOUT_SECONDS = 60

# Cap on the materialised diff so a malicious or runaway agent can't
# pin multiple GB of patch bytes in memory (three copies live at peak:
# the captured stdout buffer plus two pipe buffers for apply --check
# and apply). 100 MiB covers the largest legitimate eval diffs we've
# observed by ~10x and short-circuits to ``verifier_error`` beyond it.
_MAX_DIFF_BYTES: int = 100 * 1024 * 1024


def _is_git_repo(workspace: Path) -> bool:
    """Return True if *workspace* has a ``.git`` directory (or file)."""
    return (workspace / ".git").exists()


def _run_git(
    args: list[str],
    cwd: Path,
    *,
    label: str,
    input_bytes: bytes | None = None,
    env: dict[str, str] | None = None,
) -> tuple[bytes, str | None]:
    """Run a git subprocess with shared timeout / capture / error shape.

    Returns ``(stdout_bytes, error)`` where ``error`` is ``None`` on
    success. *label* prefixes the error string so callers don't have
    to repeat the operation name in every except block. Stderr from
    git is run through :func:`sanitize_secrets` before being bundled
    into the error string — a rejected diff can echo file contents
    back, and the error flows verbatim into ``ScoreResult.error`` and
    ``scoring.json``.
    """
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            check=True,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            input=input_bytes,
            env=env,
        )
        return result.stdout, None
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip() if exc.stderr else ""
        return b"", sanitize_secrets(f"{label} failed: {stderr or exc}")
    except (subprocess.TimeoutExpired, OSError) as exc:
        return b"", sanitize_secrets(f"{label} failed: {exc}")


def _capture_workspace_diff(
    workspace: Path, base_commit: str
) -> tuple[bytes, str | None]:
    """Capture diff bytes against *base_commit* without mutating *workspace*.

    Uses a private ``GIT_INDEX_FILE`` so the agent workspace's real
    index is never modified — repeated scoring (retries, dual scorers)
    and human follow-up inspection see the workspace exactly as the
    agent left it. The temporary index is seeded with ``base_commit``'s
    tree, then ``git add -A`` updates it to match the working tree
    (respecting ``.gitignore``). The cached diff against ``base_commit``
    is the difference between that tree-equivalent index and the base.

    The ``--binary`` flag emits binary patches as base85 so apply can
    round-trip them. Returns ``(diff_bytes, err)``; ``err`` is ``None``
    on success. Captures EVERYTHING the agent did regardless of HEAD
    position — see Slice 1b plan §"Decisions to make in Slice 1b" #1.
    """
    # NamedTemporaryFile + delete=False so we can pass the path to git
    # subprocesses without holding an open fd.
    tmp = tempfile.NamedTemporaryFile(
        prefix="codeprobe-index-", delete=False
    )
    index_path = tmp.name
    tmp.close()
    # NamedTemporaryFile creates the file; git read-tree wants it to
    # NOT pre-exist as an empty index (or it complains). Remove it,
    # then GIT_INDEX_FILE will create it fresh.
    Path(index_path).unlink(missing_ok=True)
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = index_path
    try:
        # Seed the temp index with the tree at ``base_commit`` so that
        # ``git add -A`` produces a meaningful delta. Without the seed,
        # the empty index would make every workspace file look "added"
        # and the diff would be larger than necessary (though still
        # correct after apply).
        _, err = _run_git(
            ["git", "read-tree", base_commit],
            workspace,
            label="git read-tree",
            env=env,
        )
        if err is not None:
            return b"", err
        _, err = _run_git(
            ["git", "add", "-A"],
            workspace,
            label="git add",
            env=env,
        )
        if err is not None:
            return b"", err
        return _run_git(
            ["git", "diff", "--binary", "--cached", base_commit],
            workspace,
            label="git diff",
            env=env,
        )
    finally:
        Path(index_path).unlink(missing_ok=True)


def _create_fresh_checkout(
    workspace: Path, base_commit: str, dest: Path
) -> str | None:
    """Clone *workspace* into *dest* and detach to *base_commit*.

    Uses ``--local --no-hardlinks`` so the clone is a real filesystem
    copy (hardlinks would mean an apply that modifies a file mutates
    the source workspace too). Returns ``None`` on success, an error
    string on failure.
    """
    _, err = _run_git(
        [
            "git",
            "clone",
            "--local",
            "--no-hardlinks",
            "--quiet",
            str(workspace),
            str(dest),
        ],
        # ``git clone`` doesn't actually need a real cwd, but passing
        # workspace keeps relative-path resolution predictable.
        workspace,
        label="git clone",
    )
    if err is not None:
        return err
    _, err = _run_git(
        ["git", "checkout", "--detach", "--quiet", base_commit],
        dest,
        label=f"git checkout {base_commit}",
    )
    return err


def _apply_diff(checkout: Path, diff_bytes: bytes) -> str | None:
    """Run ``git apply --check`` then ``git apply --binary`` in *checkout*.

    An empty diff is treated as a successful no-op so an agent that
    didn't modify anything still goes through the materialised path.
    Returns ``None`` on success, an error string on rejection.
    """
    if not diff_bytes.strip():
        # Empty diff — nothing to apply, fresh checkout IS the result.
        return None
    _, err = _run_git(
        ["git", "apply", "--binary", "--check", "-"],
        checkout,
        label="git apply --check",
        input_bytes=diff_bytes,
    )
    if err is not None:
        return err
    _, err = _run_git(
        ["git", "apply", "--binary", "-"],
        checkout,
        label="git apply",
        input_bytes=diff_bytes,
    )
    return err


def _materialize_workspace(
    agent_state: AgentState, sandbox_dir: Path
) -> tuple[Path | None, str | None]:
    """Run the three-step pipeline; return (checkout_path, error).

    The fresh checkout lives under ``sandbox_dir / "checkout"`` so the
    existing ``_run_in_sandbox`` cleanup reclaims it. On any failure
    return ``(None, "<reason>")`` and the caller routes to
    ``verifier_error``.
    """
    # Resolve through the package namespace at call time so tests that
    # monkeypatch ``codeprobe.core.scoring._capture_workspace_diff`` keep
    # working (the pre-package module resolved this name via module
    # globals, which the package attribute mirrored).
    from codeprobe.core import scoring as _scoring_pkg

    diff_bytes, err = _scoring_pkg._capture_workspace_diff(
        agent_state.workspace, agent_state.base_commit
    )
    if err is not None:
        return None, err
    if len(diff_bytes) > _MAX_DIFF_BYTES:
        # Fail loudly rather than try to apply a multi-GB patch. The
        # cap protects against runaway / malicious agents that stage
        # huge binary artefacts before the verifier runs.
        return None, (
            f"materialisation refused: diff size {len(diff_bytes)} bytes "
            f"exceeds limit {_MAX_DIFF_BYTES}"
        )

    checkout = sandbox_dir / "checkout"
    err = _create_fresh_checkout(
        agent_state.workspace, agent_state.base_commit, checkout
    )
    if err is not None:
        return None, err

    err = _apply_diff(checkout, diff_bytes)
    if err is not None:
        return None, err

    return checkout, None
