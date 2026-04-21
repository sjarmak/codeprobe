"""End-to-end capability coverage for ``codeprobe assess``.

Assess analyzes a git repository for benchmarking potential. We exercise
two cells of the matrix:

  - Python minimal repo    (synthetic; two-file layout)
  - EnterpriseBench repo   (real repo under /home/ds/projects/EnterpriseBench)

Assessment falls back to heuristic scoring when no LLM backend is reachable,
so tests are deterministic in CI without network or API keys.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeprobe.cli import main
from tests.capabilities.fixtures import ENTERPRISE_BENCH_ROOT


pytestmark = [pytest.mark.capability]


@pytest.fixture(autouse=True)
def _force_heuristic_scoring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force assess's deterministic heuristic path — no network, no API keys.

    External LLM mocking is allowed per the bead AC. The pipeline logic
    (``score_repo_heuristic``, ``gather_heuristics``, CLI formatting) still
    runs for real against real filesystem inputs.
    """
    monkeypatch.setattr("codeprobe.core.llm.claude_available", lambda: False)
    monkeypatch.setattr("codeprobe.core.llm.llm_available", lambda: False)


@pytest.mark.matrix
def test_assess_scores_a_python_repo(
    cli_runner,
    minimal_git_repo: Path,
) -> None:
    """Assess a fresh Python repo: must produce an overall score and rubric breakdown."""
    result = cli_runner.invoke(main, ["assess", str(minimal_git_repo)])

    assert result.exit_code == 0, (
        f"capability=assess fixture=synthetic/python exit_code={result.exit_code} "
        f"stderr={result.stderr!r} stdout={result.output!r}"
    )
    # Structural: header + breakdown keywords appear — avoid string-exact matching.
    for required_fragment in ("Codebase Assessment", "Overall Score", "Breakdown", "Recommendation"):
        assert required_fragment in result.output, (
            f"capability=assess fixture=synthetic/python "
            f"missing section {required_fragment!r}: {result.output!r}"
        )


@pytest.mark.matrix
def test_assess_rejects_non_git_directory(
    cli_runner,
    tmp_path: Path,
) -> None:
    """A directory without .git must produce an explicit error, not a silent pass."""
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()

    result = cli_runner.invoke(main, ["assess", str(not_a_repo)])

    assert result.exit_code != 0, (
        f"capability=assess fixture=synthetic/not-git expected non-zero exit, "
        f"got {result.exit_code}; stderr={result.stderr!r}"
    )
    # Error messages land on stderr; tolerate either stream for CI robustness.
    combined = (result.stderr or "") + (result.output or "")
    assert "git repository" in combined.lower() or "not a directory" in combined.lower(), (
        f"capability=assess fixture=synthetic/not-git expected a git-related error; "
        f"got stderr={result.stderr!r} stdout={result.output!r}"
    )


def test_assess_on_real_oracle_repo(cli_runner) -> None:
    """Assess the EnterpriseBench repo (real fixture) — structure-only assertions."""
    if not (ENTERPRISE_BENCH_ROOT / ".git").exists():
        pytest.skip(
            f"oracle fixture EnterpriseBench not a git repo at {ENTERPRISE_BENCH_ROOT}"
        )

    result = cli_runner.invoke(main, ["assess", str(ENTERPRISE_BENCH_ROOT)])

    assert result.exit_code == 0, (
        f"capability=assess fixture=EnterpriseBench exit_code={result.exit_code} "
        f"stderr={result.stderr!r} stdout={result.output!r}"
    )
    assert "Overall Score" in result.output, (
        f"capability=assess fixture=EnterpriseBench missing 'Overall Score' header: "
        f"{result.output!r}"
    )
