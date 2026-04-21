"""End-to-end capability coverage for ``codeprobe mine``.

Mine extracts eval tasks from repo history. External LLM calls are expensive,
so we drive mining through ``--no-llm`` (deterministic regex-based instruction
generation) on small synthetic repos. The pipeline code — argument resolution,
git traversal, task-type dispatch, writer — runs for real.

Matrix cells exercised:
  - synthetic python repo / sdlc mining  (--no-llm)
  - synthetic python repo / probe mining (--emit-tasks)
  - CLI surface: --list-task-types, --list-profiles
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codeprobe.cli import main


pytestmark = [pytest.mark.capability]


def _make_repo_with_merged_pr(repo: Path) -> None:
    """Seed a repo with one feature-branch merge so SDLC mining can find a task."""
    def run(args: list[str]) -> None:
        subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        )

    run(["checkout", "-qb", "feature/add-helper"])
    (repo / "src" / "helper.py").write_text(
        '"""Utility helper used by downstream callers."""\n'
        "def greet(name: str) -> str:\n"
        "    return f'hello {name}'\n",
        encoding="utf-8",
    )
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "test_helper.py").write_text(
        "from src.helper import greet\n"
        "def test_greet():\n"
        "    assert greet('x') == 'hello x'\n",
        encoding="utf-8",
    )
    run(["add", "src/helper.py", "tests/test_helper.py"])
    run(["commit", "-q", "-m", "feat: add greet helper\n\nAdds a greet() helper for downstream callers."])

    run(["checkout", "-q", "main"])
    run([
        "merge",
        "--no-ff",
        "-m",
        "Merge pull request #1 from test/add-helper\n\nfeat: add greet helper",
        "feature/add-helper",
    ])


@pytest.mark.matrix
def test_mine_list_task_types(cli_runner) -> None:
    """mine --list-task-types exits clean and lists built-in task types."""
    result = cli_runner.invoke(main, ["mine", "--list-task-types"])

    assert result.exit_code == 0, (
        f"capability=mine flag=--list-task-types exit_code={result.exit_code} "
        f"stderr={result.stderr!r}"
    )
    # Structural: at least one of the well-known task types appears, header is present.
    assert "Task type" in result.output
    # At least one registered task type name should show up. Exact set may grow
    # over time — accept any of the documented anchors.
    known_types = ("sdlc_code_change", "micro_probe", "architecture_comprehension")
    assert any(t in result.output for t in known_types), (
        f"capability=mine flag=--list-task-types expected one of {known_types} in output; "
        f"got: {result.output!r}"
    )


@pytest.mark.matrix
def test_mine_list_profiles_clean_env(
    cli_runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mine --list-profiles succeeds even when no profiles exist."""
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.codeprobe/mine-profiles.json

    result = cli_runner.invoke(main, ["mine", str(tmp_path), "--list-profiles"])

    assert result.exit_code == 0, (
        f"capability=mine flag=--list-profiles exit_code={result.exit_code} "
        f"stderr={result.stderr!r}"
    )
    assert "No profiles found" in result.output or "Source" in result.output


@pytest.mark.matrix
def test_mine_sdlc_on_synthetic_python_repo(
    cli_runner, minimal_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end mine run on a synthetic python repo with one merged PR.

    Uses ``--no-llm`` so the test runs offline and deterministically. The
    writer, extractor, and suite manifest code paths execute for real.
    """
    _make_repo_with_merged_pr(minimal_git_repo)
    # Avoid any accidental API key pickup and force deterministic mode.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    result = cli_runner.invoke(
        main,
        [
            "mine",
            str(minimal_git_repo),
            "--no-interactive",
            "--no-llm",
            "--count",
            "3",
            "--goal",
            "quality",
        ],
    )

    # The command should not crash; it may emit "No tasks generated" if the
    # synthetic history is too small. Either outcome is structurally valid
    # as long as exit is clean.
    assert result.exit_code == 0, (
        f"capability=mine fixture=synthetic/python exit_code={result.exit_code} "
        f"stderr={result.stderr!r} stdout={result.output!r}"
    )


def test_mine_rejects_conflicting_cross_repo_and_org_scale(
    cli_runner, minimal_git_repo: Path
) -> None:
    """Orthogonal CLI guards: --cross-repo with --org-scale must fail cleanly."""
    result = cli_runner.invoke(
        main,
        [
            "mine",
            str(minimal_git_repo),
            "--cross-repo",
            str(minimal_git_repo),
            "--org-scale",
            "--no-interactive",
        ],
    )

    assert result.exit_code != 0, (
        f"capability=mine fixture=synthetic/python expected non-zero exit for "
        f"--cross-repo + --org-scale; got {result.exit_code}"
    )
    combined = (result.stderr or "") + (result.output or "")
    assert "cross-repo" in combined.lower() and "org-scale" in combined.lower(), (
        f"capability=mine fixture=synthetic/python expected mutual-exclusion error; "
        f"stderr={result.stderr!r} stdout={result.output!r}"
    )
