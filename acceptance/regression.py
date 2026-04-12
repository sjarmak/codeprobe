"""Regression gate for the acceptance loop.

Runs pytest, ruff, and mypy in sequence after each Fix Agent commit. If any
check fails, reverts the most recent commit via ``git revert HEAD --no-edit``
so the Fix Agent's broken change never enters the working history, and
returns a structured :class:`RegressionResult` that the orchestrator can log.

This module is ZFC-compliant: every decision is a deterministic exit-code
comparison on tools the project already trusts (pytest/ruff/mypy). No
semantic judgment, no heuristics, no LLM calls.

Design notes
------------

- **Check order is pytest → ruff → mypy.** Failing tests are the most urgent
  signal (they indicate a broken product) and are also the slowest check,
  so we run them first to fail fast on semantic breakage before spending
  time on style/type checks. Order is intentional, not arbitrary.

- **Subprocess capture is pooled into a single ``output`` string on failure.**
  The Fix Agent only needs to see what broke, not the full pass log. On
  success ``output`` is the empty string — callers should not log it.

- **Revert uses ``git revert HEAD --no-edit``** rather than ``git reset --hard``
  so the trail in git history still shows the Fix Agent's attempt and the
  subsequent revert. This is auditability-by-default; the orchestrator may
  override with ``revert_on_failure=False`` for dry-run inspection.

- **Revert failure is surfaced, not swallowed.** If the revert itself fails,
  ``reverted`` stays ``False`` and the revert subprocess output is appended
  to ``output``. Callers can then decide whether to abort the loop or ask a
  human to untangle the working tree.

Usage
-----

From Python::

    from acceptance.regression import run_regression_gate
    result = run_regression_gate(Path("/home/ds/projects/codeprobe"))
    if not result.passed:
        print(f"Failed: {result.failed_check}; reverted={result.reverted}")

From the CLI (called by the Fix Agent prompt)::

    python3 -m acceptance.regression --repo-root /home/ds/projects/codeprobe

Exit codes:

- ``0`` — all checks passed
- ``1`` — at least one check failed (revert attempted if enabled)
- ``2`` — invalid arguments or repo-root not a git repo
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

#: Canonical names for the three checks in the gate. The Literal keeps type
#: checkers honest at call sites that branch on ``failed_check``.
CheckName = Literal["pytest", "ruff", "mypy"]

#: Default per-command timeout. Long enough for a real test suite, short
#: enough that a hung mypy/pytest does not stall the acceptance loop.
DEFAULT_CHECK_TIMEOUT_S: float = 900.0  # 15 minutes

#: Default pytest arguments. Callers may override; the default enforces the
#: project's minimum coverage bar so a commit that silently drops coverage
#: still trips the gate.
DEFAULT_PYTEST_ARGS: tuple[str, ...] = (
    "tests/",
    "--cov=src/codeprobe",
    "--cov-fail-under=80",
)

#: Default ruff arguments. Checks both the library and tests because a
#: Fix Agent may legitimately edit either.
DEFAULT_RUFF_ARGS: tuple[str, ...] = ("check", "src/", "tests/")

#: Default mypy arguments. Restricted to the library source — mypy on the
#: test suite would require stub installs most projects skip.
DEFAULT_MYPY_ARGS: tuple[str, ...] = ("src/codeprobe",)

#: Path prefixes ruff is scoped to when ``scope_to_diff=True``. A changed
#: .py file outside these prefixes is ignored by ruff's incremental check.
_RUFF_SCOPE_PREFIXES: tuple[str, ...] = ("src/", "tests/")

#: Path prefixes mypy is scoped to when ``scope_to_diff=True``. mypy only
#: runs against library source even in diff-scoped mode.
_MYPY_SCOPE_PREFIXES: tuple[str, ...] = ("src/codeprobe/",)


@dataclass(frozen=True)
class RegressionResult:
    """Immutable outcome of a single :func:`run_regression_gate` invocation.

    Attributes
    ----------
    passed:
        ``True`` if pytest, ruff, and mypy all exited with status 0.
    failed_check:
        Name of the first failing check (``"pytest"``, ``"ruff"``, or
        ``"mypy"``) or ``None`` on success.
    reverted:
        ``True`` when the gate successfully ran ``git revert HEAD --no-edit``
        after a failure. ``False`` on success, when ``revert_on_failure`` is
        disabled, or when the revert itself failed.
    output:
        Captured stdout+stderr from the failing check (or from the revert if
        the revert failed). Empty string on success — do not log it.
    """

    passed: bool
    failed_check: str | None
    reverted: bool
    output: str


def run_regression_gate(
    repo_root: Path,
    *,
    revert_on_failure: bool = True,
    pytest_args: tuple[str, ...] | None = None,
    ruff_args: tuple[str, ...] | None = None,
    mypy_args: tuple[str, ...] | None = None,
    scope_to_diff: bool = True,
    timeout_s: float = DEFAULT_CHECK_TIMEOUT_S,
) -> RegressionResult:
    """Run pytest, ruff, mypy in sequence; revert ``HEAD`` on failure.

    The gate's job is to catch **regressions introduced by the most recent
    commit**, not to police pre-existing debt. When ``scope_to_diff`` is
    ``True`` (the default), ruff and mypy run only against .py files that
    differ between ``HEAD~1`` and ``HEAD`` — a Fix Agent commit that does
    not touch any in-scope file skips the linter/type-checker entirely.
    pytest always runs the full suite because a fix can break tests in
    unrelated modules. Explicit ``ruff_args`` / ``mypy_args`` override
    diff-scoping entirely.

    Args:
        repo_root: Absolute path to the git repository the Fix Agent just
            committed into. Must contain a ``.git`` directory.
        revert_on_failure: When ``True`` (the default), a failing check
            triggers ``git revert HEAD --no-edit`` so the broken commit is
            rolled back. When ``False``, the function returns a failure
            result without touching history (used for dry-run inspection).
        pytest_args: Tuple of arguments passed to ``pytest``. When
            ``None`` (default), uses :data:`DEFAULT_PYTEST_ARGS`.
        ruff_args: Tuple of arguments passed to ``ruff``. When ``None``
            and ``scope_to_diff`` is True, the gate computes the argument
            list from the HEAD diff. When ``None`` and ``scope_to_diff``
            is False, uses :data:`DEFAULT_RUFF_ARGS`.
        mypy_args: Same semantics as ``ruff_args`` but for mypy, scoped
            to :data:`_MYPY_SCOPE_PREFIXES`.
        scope_to_diff: When True (default), ruff and mypy args are derived
            from ``git diff --name-only HEAD~1 HEAD`` filtered to each
            tool's scope prefixes. A check with no in-scope changes is
            skipped. When the diff is unavailable (initial commit,
            detached HEAD, no parent) the gate falls back to the
            ``DEFAULT_*_ARGS`` full-tree behavior so nothing is silently
            un-checked. Ignored for any tool whose ``*_args`` is supplied
            explicitly.
        timeout_s: Per-check subprocess timeout in seconds. A timeout is
            treated as a failure of the current check.

    Returns:
        :class:`RegressionResult` describing which check (if any) failed,
        whether the revert fired, and the captured output of the failing
        check.

    Raises:
        FileNotFoundError: ``repo_root`` does not exist.
        ValueError: ``repo_root`` exists but is not a git repository.
    """
    if not repo_root.exists():
        raise FileNotFoundError(f"repo_root does not exist: {repo_root}")
    if not (repo_root / ".git").exists():
        raise ValueError(f"repo_root is not a git repository: {repo_root}")

    resolved_pytest: tuple[str, ...] = (
        pytest_args if pytest_args is not None else DEFAULT_PYTEST_ARGS
    )
    resolved_ruff = _resolve_scoped_args(
        explicit=ruff_args,
        scope_to_diff=scope_to_diff,
        repo_root=repo_root,
        scope_prefixes=_RUFF_SCOPE_PREFIXES,
        default=DEFAULT_RUFF_ARGS,
        prefix_flags=("check",),
    )
    resolved_mypy = _resolve_scoped_args(
        explicit=mypy_args,
        scope_to_diff=scope_to_diff,
        repo_root=repo_root,
        scope_prefixes=_MYPY_SCOPE_PREFIXES,
        default=DEFAULT_MYPY_ARGS,
        prefix_flags=(),
    )

    checks: list[tuple[CheckName, tuple[str, ...]]] = [
        ("pytest", (sys.executable, "-m", "pytest", *resolved_pytest)),
    ]
    if resolved_ruff is not None:
        checks.append(("ruff", (sys.executable, "-m", "ruff", *resolved_ruff)))
    if resolved_mypy is not None:
        checks.append(("mypy", (sys.executable, "-m", "mypy", *resolved_mypy)))

    for name, cmd in checks:
        returncode, output = _run_check(cmd, cwd=repo_root, timeout_s=timeout_s)
        if returncode != 0:
            reverted, revert_output = _maybe_revert(
                repo_root, enabled=revert_on_failure
            )
            combined = (
                output
                if reverted or not revert_output
                else (f"{output}\n--- git revert output ---\n{revert_output}")
            )
            return RegressionResult(
                passed=False,
                failed_check=name,
                reverted=reverted,
                output=combined,
            )

    return RegressionResult(
        passed=True,
        failed_check=None,
        reverted=False,
        output="",
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _changed_py_files(
    repo_root: Path,
    scope_prefixes: tuple[str, ...],
) -> list[str] | None:
    """Return .py files changed in ``HEAD`` vs ``HEAD~1`` matching a prefix.

    Returns ``None`` when the diff cannot be computed (initial commit, no
    parent, detached HEAD with no ancestor, git subprocess failure). The
    caller interprets ``None`` as "no baseline — fall back to full-tree".

    Returns an empty list when the diff is available but no changed file
    falls within ``scope_prefixes``. The caller interprets this as "the
    commit touched nothing this tool cares about, skip it."

    Deleted files are excluded via ``--diff-filter=AMR`` so the returned
    paths can be passed directly to ruff / mypy without further checks.
    """
    try:
        proc = subprocess.run(
            [
                "git",
                "diff",
                "--name-only",
                "--diff-filter=AMR",
                "HEAD~1",
                "HEAD",
            ],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=30.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    scoped: list[str] = []
    for line in proc.stdout.splitlines():
        path = line.strip()
        if not path.endswith(".py"):
            continue
        if not any(path.startswith(p) for p in scope_prefixes):
            continue
        scoped.append(path)
    return scoped


def _resolve_scoped_args(
    *,
    explicit: tuple[str, ...] | None,
    scope_to_diff: bool,
    repo_root: Path,
    scope_prefixes: tuple[str, ...],
    default: tuple[str, ...],
    prefix_flags: tuple[str, ...],
) -> tuple[str, ...] | None:
    """Resolve a linter/type-checker argument tuple for the gate.

    Returns:
        - ``explicit`` unchanged if the caller supplied it.
        - ``default`` when ``scope_to_diff`` is False or the diff cannot
          be computed (no baseline).
        - ``(*prefix_flags, *changed_files)`` when diff scoping succeeded
          and at least one in-scope file changed.
        - ``None`` when diff scoping succeeded but zero in-scope files
          changed; the caller should skip this check entirely.
    """
    if explicit is not None:
        return explicit
    if not scope_to_diff:
        return default
    changed = _changed_py_files(repo_root, scope_prefixes)
    if changed is None:
        return default
    if not changed:
        return None
    return (*prefix_flags, *changed)


def _run_check(
    cmd: tuple[str, ...],
    *,
    cwd: Path,
    timeout_s: float,
) -> tuple[int, str]:
    """Run ``cmd`` in ``cwd`` and return (returncode, combined_output).

    A timeout or missing executable is treated as a non-zero exit so the
    caller can uniformly branch on "did this check pass?". The returned
    output is always a string — on timeout it contains the captured partial
    output plus a trailing ``TIMEOUT after Ns`` marker so the Fix Agent can
    see what happened.
    """
    try:
        proc = subprocess.run(
            list(cmd),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        partial = ""
        if exc.stdout:
            partial += (
                exc.stdout
                if isinstance(exc.stdout, str)
                else exc.stdout.decode(errors="replace")
            )
        if exc.stderr:
            partial += (
                exc.stderr
                if isinstance(exc.stderr, str)
                else exc.stderr.decode(errors="replace")
            )
        return 124, f"{partial}\nTIMEOUT after {timeout_s}s running: {shlex.join(cmd)}"
    except FileNotFoundError as exc:
        return 127, f"command not found: {shlex.join(cmd)}: {exc}"

    combined = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, combined


def _maybe_revert(repo_root: Path, *, enabled: bool) -> tuple[bool, str]:
    """Attempt ``git revert HEAD --no-edit`` when ``enabled`` is True.

    Returns a tuple ``(reverted, output)`` where ``reverted`` is ``True`` iff
    the revert subprocess exited with status 0. The output is always the
    captured stdout+stderr of the revert command so callers can surface the
    failure to humans; on disabled revert it is the empty string.
    """
    if not enabled:
        return False, ""
    try:
        proc = subprocess.run(
            ["git", "revert", "HEAD", "--no-edit"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=60.0,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return False, f"git revert failed to launch: {exc}"
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, output


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m acceptance.regression",
        description=(
            "Run the regression gate (pytest, ruff, mypy) against a repo "
            "and revert HEAD on failure."
        ),
    )
    parser.add_argument(
        "--repo-root",
        required=True,
        type=Path,
        help="Absolute path to the git repo to check.",
    )
    parser.add_argument(
        "--no-revert",
        action="store_true",
        help="Disable automatic git revert on failure (dry-run).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    repo_root: Path = args.repo_root.resolve()
    try:
        result = run_regression_gate(
            repo_root,
            revert_on_failure=not args.no_revert,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if result.passed:
        print("REGRESSION-GATE PASSED")
        return 0

    print(
        f"REGRESSION-GATE FAILED check={result.failed_check} reverted={result.reverted}"
    )
    if result.output:
        print("--- captured output ---")
        print(result.output)
    return 1


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(main())


__all__ = [
    "CheckName",
    "DEFAULT_CHECK_TIMEOUT_S",
    "DEFAULT_MYPY_ARGS",
    "DEFAULT_PYTEST_ARGS",
    "DEFAULT_RUFF_ARGS",
    "RegressionResult",
    "main",
    "run_regression_gate",
]
