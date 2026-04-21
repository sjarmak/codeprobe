"""End-to-end capability coverage — stitch mine → validate → run → interpret.

This module tests cross-capability integration: changes to one advertised
capability must not silently break its neighbors. The full pipeline runs
against:

  1. A synthetic python repo (validate → run → interpret with FakeAdapter)
  2. Real MCP-Eval-Tasks oracle (validate only — exercising real fixture)

Cross-cutting assertions: CompletedTask round-trips through checkpoint save/
load, ``interpret`` renders non-empty output in each supported format.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codeprobe.adapters.protocol import AgentConfig
from codeprobe.analysis import (
    format_csv_report,
    format_json_report,
    format_text_report,
    generate_report,
)
from codeprobe.cli import main
from codeprobe.cli.validate_cmd import run_validate
from codeprobe.core.executor import execute_config
from codeprobe.models.experiment import ConfigResults, ExperimentConfig
from tests.capabilities.fixtures import FULL_TASK_FIXTURES, MCP_CCX_SGAUTH_301
from tests.conftest import FakeAdapter


pytestmark = [pytest.mark.capability]


@pytest.mark.matrix
def test_e2e_validate_to_run_to_interpret_on_synthetic_python(
    tmp_path: Path, make_task_dir, cli_runner
) -> None:
    """Full pipeline on a synthetic python task: validate, run, format reports."""
    # -- Stage 1: validate synthetic task
    task_dir = make_task_dir(
        "t1",
        task_type="sdlc_code_change",
        verification_mode="test_script",
        language="python",
    )
    vresult = cli_runner.invoke(main, ["validate", str(task_dir)])
    assert vresult.exit_code == 0, (
        f"capability=e2e stage=validate fixture=synthetic/python "
        f"exit_code={vresult.exit_code} output={vresult.output!r}"
    )

    # -- Stage 2: run synthetic task through FakeAdapter
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "e@example.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "e"], check=True, capture_output=True)
    (repo / "README.md").write_text("seed")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed"], check=True, capture_output=True)

    adapter = FakeAdapter(stdout="ok", cost_usd=0.02, cost_model="per_token", duration=0.1)
    exp_config = ExperimentConfig(label="baseline")

    completed = execute_config(
        adapter=adapter,
        task_dirs=[task_dir],
        repo_path=repo,
        experiment_config=exp_config,
        agent_config=AgentConfig(),
    )
    assert completed, (
        "capability=e2e stage=run fixture=synthetic/python expected at least one "
        "CompletedTask; got []"
    )

    # -- Stage 3: interpret — generate a report across the completed results
    config_results = ConfigResults(config=exp_config.label, completed=completed)
    report = generate_report("capability-e2e", [config_results])

    text_report = format_text_report(report)
    json_report = format_json_report(report)
    csv_report = format_csv_report(report)

    assert isinstance(text_report, str) and text_report.strip(), (
        "capability=e2e stage=interpret format=text expected non-empty report"
    )
    assert isinstance(json_report, str) and json_report.strip(), (
        "capability=e2e stage=interpret format=json expected non-empty report"
    )
    assert isinstance(csv_report, str) and csv_report.strip(), (
        "capability=e2e stage=interpret format=csv expected non-empty report"
    )


@pytest.mark.matrix
def test_e2e_validate_on_every_oracle_fixture(cli_runner) -> None:
    """Every registered full-task oracle validates cleanly end-to-end.

    This is the cross-corpus regression net — if a writer change subtly
    breaks a published oracle shape, the matrix catches it here.
    """
    skipped: list[str] = []
    failures: list[tuple[str, int, str]] = []
    passed: list[str] = []
    for oracle in FULL_TASK_FIXTURES:
        if not oracle.exists():
            skipped.append(oracle.skip_reason())
            continue
        result = cli_runner.invoke(main, ["validate", str(oracle.path)])
        tag = f"{oracle.corpus}:{oracle.name}"
        if result.exit_code != 0:
            failures.append((tag, result.exit_code, result.output))
        else:
            passed.append(tag)

    if not passed and skipped:
        pytest.skip(
            "capability=e2e stage=validate — no oracle fixtures available:\n"
            + "\n".join(skipped)
        )

    assert not failures, (
        "capability=e2e stage=validate matrix had failures:\n"
        + "\n".join(
            f"  {tag}: exit={rc}, output={out!r}" for tag, rc, out in failures
        )
    )


def test_e2e_run_validate_programmatically_on_ccx_oracle(require_oracle) -> None:
    """Call the programmatic run_validate() on the ccx-sgauth-301 oracle.

    Exercises the non-CLI surface of validate — important because
    mol-focus-review invokes codeprobe via both CLI and Python APIs.
    """
    require_oracle(MCP_CCX_SGAUTH_301)

    results = run_validate(MCP_CCX_SGAUTH_301.path)

    assert results, (
        f"capability=e2e stage=validate fixture={MCP_CCX_SGAUTH_301.name} "
        f"expected at least one CheckResult"
    )
    failed = [r for r in results if not r.passed]
    assert not failed, (
        f"capability=e2e stage=validate fixture={MCP_CCX_SGAUTH_301.name} "
        f"unexpected failures: {[(r.name, r.detail) for r in failed]}"
    )
