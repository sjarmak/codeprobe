"""End-to-end capability coverage for ``codeprobe validate``.

What this module proves:
  - ``codeprobe validate`` exits cleanly (rc 0) on structurally valid tasks.
  - It fails loudly with a non-zero exit and an informative message on
    malformed tasks.
  - Real oracle task directories under /home/ds/projects/MCP-Eval-Tasks/
    pass the same validation a user would run locally.

Matrix cells exercised here:
  - MCP-Eval-Tasks / go  / compliance-audit (ccx-sgauth-301)
  - MCP-Eval-Tasks / go  / anchor-fix       (sg-deepsearch-anchor-fix-001)
  - synthetic     / python / sdlc_code_change
  - synthetic     / python / dual verification mode

Aggregate across the module: 2 languages × 2 task types × 2+ oracles.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeprobe.cli import main
from codeprobe.cli.validate_cmd import run_validate
from tests.capabilities.fixtures import (
    FULL_TASK_FIXTURES,
    OracleFixture,
)


pytestmark = [pytest.mark.capability]


@pytest.mark.parametrize(
    "oracle",
    FULL_TASK_FIXTURES,
    ids=lambda f: f"{f.corpus}:{f.name}",
)
@pytest.mark.matrix
def test_validate_passes_on_real_oracle_task(
    oracle: OracleFixture,
    require_oracle,
    cli_runner,
) -> None:
    """Validate should accept a production oracle task without failure."""
    require_oracle(oracle)

    result = cli_runner.invoke(main, ["validate", str(oracle.path)])

    assert result.exit_code == 0, (
        f"capability=validate fixture={oracle.corpus}/{oracle.name} "
        f"exit_code={result.exit_code} stderr={result.stderr!r} stdout={result.output!r}"
    )
    # Structural: every check line starts with 'PASS' or 'FAIL'; expect PASS only.
    assert "  PASS  " in result.output, (
        f"capability=validate fixture={oracle.corpus}/{oracle.name} "
        f"expected at least one PASS line, got: {result.output!r}"
    )
    assert "  FAIL  " not in result.output, (
        f"capability=validate fixture={oracle.corpus}/{oracle.name} "
        f"unexpected FAIL line in output: {result.output!r}"
    )


def test_validate_fails_on_missing_instruction(
    cli_runner,
    make_task_dir,
    tmp_path: Path,
) -> None:
    """A task missing instruction.md must exit non-zero with an actionable message."""
    task_dir = make_task_dir("bad-task", language="python")
    (task_dir / "instruction.md").unlink()

    result = cli_runner.invoke(main, ["validate", str(task_dir)])

    assert result.exit_code != 0, (
        f"capability=validate fixture=synthetic/bad-task expected non-zero exit, "
        f"got {result.exit_code}; output: {result.output!r}"
    )
    assert "instruction.md" in result.output, (
        f"capability=validate fixture=synthetic/bad-task expected instruction.md "
        f"to be named in output; got: {result.output!r}"
    )


def test_validate_accepts_synthetic_sdlc_task(
    cli_runner,
    make_task_dir,
) -> None:
    """Synthetic task in sdlc_code_change mode passes validate."""
    task_dir = make_task_dir(
        "ok-sdlc",
        task_type="sdlc_code_change",
        verification_mode="test_script",
        language="python",
    )

    result = cli_runner.invoke(main, ["validate", str(task_dir)])

    assert result.exit_code == 0, (
        f"capability=validate fixture=synthetic/ok-sdlc exit_code={result.exit_code} "
        f"stderr={result.stderr!r}"
    )


def test_validate_accepts_dual_mode_task(
    cli_runner,
    make_task_dir,
) -> None:
    """Dual-mode task with ground_truth answer passes validate."""
    task_dir = make_task_dir(
        "ok-dual",
        task_type="architecture_comprehension",
        verification_mode="dual",
        language="python",
        ground_truth={
            "schema_version": 1,
            "answer_type": "file_list",
            "answer": ["src/a.py", "src/b.py"],
        },
    )

    result = cli_runner.invoke(main, ["validate", str(task_dir)])

    assert result.exit_code == 0, (
        f"capability=validate fixture=synthetic/ok-dual exit_code={result.exit_code} "
        f"stderr={result.stderr!r} stdout={result.output!r}"
    )


def test_run_validate_returns_structured_check_results(
    make_task_dir,
) -> None:
    """Programmatic API returns CheckResult objects — each has name/passed/detail.

    This backs the reviewer-facing claim that the capability's *internal*
    surface (not just CLI exit codes) is exercised end-to-end.
    """
    task_dir = make_task_dir("programmatic", language="python")

    results = run_validate(task_dir)

    assert results, "capability=validate expected at least one CheckResult"
    for r in results:
        assert hasattr(r, "name"), "CheckResult missing 'name' attribute"
        assert hasattr(r, "passed"), "CheckResult missing 'passed' attribute"
        assert hasattr(r, "detail"), "CheckResult missing 'detail' attribute"
        assert isinstance(r.name, str) and r.name
        assert isinstance(r.passed, bool)
        assert isinstance(r.detail, str)
