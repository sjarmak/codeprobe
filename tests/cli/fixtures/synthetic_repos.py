"""Builders for synthetic local git repos used by the bare-invocation matrix.

Each ``make_*`` builder produces a ready-to-mine repo under a caller-provided
``tmp_path``. All repos are fully local — no external clones, no network
access, no dependence on ``gh`` being installed or configured.

The builders aim to cover the narrative-source taxonomy from PRD §M1:

* ``make_pr_rich`` — a repo with simulated merged PR commits.
* ``make_commits_only`` — linear history, no merges, no PRs.
* ``make_auto_squash_heavy`` — 100+ squash commits + 5 RFCs under ``docs/rfcs/``.
* ``make_issue_tracked`` — commits referencing ``#<id>`` issue markers.
* ``make_airgapped_env`` — commits-only repo + monkey-patched
  ``has_pr_narratives`` to force the no-PR code path.

Also exposes :func:`patch_adapter` — a ``monkeypatch`` helper that swaps the
adapter registry's ``resolve()`` for a :class:`FakeAdapter` instance so
``codeprobe run`` does not hit the network or invoke a real coding agent.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from codeprobe.adapters.protocol import AgentConfig, AgentOutput


# -- git helpers --------------------------------------------------------------


_GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@test",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@test",
    "GIT_AUTHOR_DATE": "2024-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2024-01-01T00:00:00Z",
    # Keep PATH so git itself is reachable.
    "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
    # Prevent user/global config from leaking in.
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "HOME": "/tmp",
}


def _git(repo: Path, *args: str) -> str:
    """Run ``git *args`` in *repo* with a deterministic env. Return stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )
    return result.stdout.strip()


def _init_repo(repo: Path) -> None:
    """Create a fresh repo and write a sentinel README."""
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("synthetic\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "chore: init")


# -- builders -----------------------------------------------------------------


def make_pr_rich(tmp_path: Path, *, n_prs: int = 5) -> Path:
    """Build a repo with *n_prs* simulated merged PRs on ``main``.

    Each PR is a dedicated branch with a single commit that we merge back
    using ``--no-ff`` so we end up with a merge commit on ``main``. There
    is no remote, so ``gh pr list`` will still return empty — mining
    behavior for this shape reflects local-only PR topology.
    """
    repo = tmp_path / "pr_rich"
    _init_repo(repo)

    for i in range(1, n_prs + 1):
        branch = f"pr/{i}"
        _git(repo, "checkout", "-q", "-b", branch)
        feature_file = repo / f"feature_{i}.py"
        feature_file.write_text(
            f'"""Feature {i}."""\n\n\ndef feature_{i}():\n    return {i}\n'
        )
        _git(repo, "add", ".")
        _git(
            repo,
            "commit",
            "-q",
            "-m",
            f"feat: add feature {i}\n\nImplements feature {i}.",
        )
        _git(repo, "checkout", "-q", "main")
        _git(
            repo,
            "merge",
            "--no-ff",
            "-q",
            "-m",
            f"Merge PR #{i}: add feature {i}",
            branch,
        )

    return repo


def make_commits_only(tmp_path: Path, *, n_commits: int = 20) -> Path:
    """Build a repo with a linear history of *n_commits* commits — no PRs."""
    repo = tmp_path / "commits_only"
    _init_repo(repo)

    for i in range(1, n_commits + 1):
        (repo / f"file_{i}.py").write_text(f"value = {i}\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", f"feat: add file_{i}")

    return repo


def make_auto_squash_heavy(
    tmp_path: Path, *, n_prs: int = 100, n_rfcs: int = 5
) -> Path:
    """Build a squash-merge-heavy repo with *n_prs* empty-body squash commits.

    Also writes *n_rfcs* markdown files under ``docs/rfcs/`` so the RFC
    narrative adapter has content to work with.
    """
    repo = tmp_path / "auto_squash_heavy"
    _init_repo(repo)

    # Write the RFC batch up-front so the auto-squash commits have real
    # content too.
    rfcs_dir = repo / "docs" / "rfcs"
    rfcs_dir.mkdir(parents=True)
    for i in range(1, n_rfcs + 1):
        rfc = rfcs_dir / f"{i:04d}-design-note.md"
        rfc.write_text(
            f"# RFC {i}: design note {i}\n\n"
            f"This RFC describes a design decision for subsystem {i}.\n"
        )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", f"docs: add {n_rfcs} RFCs")

    # Simulate n_prs squash-merges with thin bodies.
    for i in range(1, n_prs + 1):
        target = repo / f"src_{i % 10}.py"
        prior = target.read_text() if target.exists() else ""
        target.write_text(prior + f"# change {i}\n")
        _git(repo, "add", ".")
        # Squash commit message pattern: subject only, no body.
        _git(repo, "commit", "-q", "-m", f"fix: #{i} squashed")

    return repo


def make_issue_tracked(tmp_path: Path, *, n_commits: int = 15) -> Path:
    """Build a repo whose commit messages reference issue IDs like ``#123``."""
    repo = tmp_path / "issue_tracked"
    _init_repo(repo)

    for i in range(1, n_commits + 1):
        (repo / f"mod_{i}.py").write_text(f"MARKER = {i}\n")
        _git(repo, "add", ".")
        _git(
            repo,
            "commit",
            "-q",
            "-m",
            f"fix: resolve #{100 + i} — patch module {i}",
        )

    return repo


def make_airgapped_env(tmp_path: Path, monkeypatch: Any) -> Path:
    """Build a commits-only repo and force the no-PR-narratives code path.

    ``has_pr_narratives`` is replaced with a stub that always returns
    ``False`` regardless of whether ``gh`` is installed or the repo has
    a remote. Simulates a true airgapped dev box where PR metadata is
    provably unavailable.
    """
    repo = make_commits_only(tmp_path / "airgap_sub", n_commits=10)

    # Stub has_pr_narratives at the import site used by the mine CLI.
    monkeypatch.setattr(
        "codeprobe.mining.sources.has_pr_narratives",
        lambda path, timeout=10: False,
    )
    # The CLI imports it via the local reference in mine_cmd.py's
    # _resolve_narrative_source. That function does
    # ``from codeprobe.mining.sources import has_pr_narratives`` inside
    # the function body, so patching the attribute on the module object
    # is sufficient — no second patch location needed.

    return repo


# -- FakeAdapter --------------------------------------------------------------


class FakeAdapter:
    """Minimal in-memory AgentAdapter for baseline-harness runs.

    Mirrors the contract expected by :mod:`codeprobe.core.executor` and
    :mod:`codeprobe.core.registry`. Returns a fixed :class:`AgentOutput`
    with a deterministic ``cost_usd`` so downstream totals are stable
    across baseline regenerations.
    """

    name = "fake"

    def preflight(self, config: AgentConfig) -> list[str]:  # noqa: ARG002
        return []

    def find_binary(self) -> str | None:
        return "/usr/bin/true"

    def build_command(self, prompt: str, config: AgentConfig) -> list[str]:  # noqa: ARG002
        return ["true"]

    def run(
        self,
        prompt: str,  # noqa: ARG002
        config: AgentConfig,  # noqa: ARG002
        session_env: dict[str, str] | None = None,  # noqa: ARG002
    ) -> AgentOutput:
        return AgentOutput(
            stdout="",
            stderr=None,
            exit_code=0,
            duration_seconds=0.0,
            cost_usd=0.0,
            cost_model="per_token",
            cost_source="estimated",
            input_tokens=0,
            output_tokens=0,
        )

    def isolate_session(self, slot_id: int) -> dict[str, str]:  # noqa: ARG002
        return {}


def patch_adapter(monkeypatch: Any) -> FakeAdapter:
    """Install the :class:`FakeAdapter` into the agent registry.

    Patches both ``codeprobe.core.registry.resolve`` and the already-bound
    reference in ``codeprobe.cli.run_cmd`` (imported at module load time).
    Returns the adapter instance so callers can inspect ``run`` calls.
    """
    fake = FakeAdapter()

    def _resolve(name: str) -> FakeAdapter:  # noqa: ARG001
        return fake

    monkeypatch.setattr("codeprobe.core.registry.resolve", _resolve)
    # run_cmd.py does ``from codeprobe.core.registry import resolve`` at
    # module scope, so the rebind above is invisible there. Patch the
    # local name too.
    monkeypatch.setattr("codeprobe.cli.run_cmd.resolve", _resolve)
    return fake


# -- experiment helper --------------------------------------------------------


def write_minimal_experiment(repo: Path) -> Path:
    """Create a minimal ``experiment.json`` under ``<repo>/.codeprobe/``.

    ``run_eval`` expects this to exist; with zero configs it auto-creates
    a default one when ``--agent`` is passed. The task list is empty so
    ``run`` will fall through to the "No tasks found" path unless mining
    produced any — which is exactly the behavior the baseline wants to
    capture.
    """
    import json

    codeprobe_dir = repo / ".codeprobe"
    codeprobe_dir.mkdir(parents=True, exist_ok=True)
    exp = {
        "name": "baseline",
        "description": "bare-invocation matrix",
        "tasks_dir": "tasks",
        "configs": [],
        "task_ids": [],
    }
    exp_path = codeprobe_dir / "experiment.json"
    exp_path.write_text(json.dumps(exp))
    return exp_path


__all__ = [
    "FakeAdapter",
    "make_airgapped_env",
    "make_auto_squash_heavy",
    "make_commits_only",
    "make_issue_tracked",
    "make_pr_rich",
    "patch_adapter",
    "write_minimal_experiment",
]
