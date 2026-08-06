"""Keep the integration marker documentation aligned with CI collection."""

from __future__ import annotations

import shlex
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
CI_WORKFLOWS = (
    REPO_ROOT / ".github/workflows/ci.yml",
    REPO_ROOT / ".github/workflows/ci-latest.yml",
)
INTEGRATION_POLICY_DOCS = {
    REPO_ROOT / "tests/llm/test_parity.py": (
        "default pytest and CI runs collect it"
    ),
    REPO_ROOT / "tests/test_packaging.py": (
        "included in default pytest and CI runs"
    ),
    REPO_ROOT / "tests/test_release_gate.py": (
        "included in default pytest and CI runs"
    ),
}
# Jobs allowed to run a narrowed pytest command, mapped to the one target
# they may narrow to. Platform-supplement jobs cannot run the whole suite —
# the macOS runner has no container engine and the suite assumes Linux in
# places — but a narrowed run must never be mistaken for the full-suite gate.
# Hence an explicit per-job target rather than a blanket exemption: adding a
# job here is a visible decision, and widening its scope fails this test.
PLATFORM_SUPPLEMENT_JOBS = {
    "mining-macos": "tests/mining/",
}
STALE_OPT_IN_CLAIMS = (
    "skipped by default",
    "integration, opt-in",
    "pragma: no cover - opt-in",
    "when explicitly requested",
)
PYTEST_COLLECTION_FILTERS = (
    "-k",
    "-m",
    "--co",
    "--collect-only",
    "--deselect",
    "--ignore",
    "--ignore-glob",
    "--markers",
)


@pytest.mark.parametrize(
    "addopts",
    (
        '-m "not integration"',
        ["-m", "not integration"],
        ["--ignore", "tests/llm"],
        ["tests/unit"],
        ["--collect-only", "tests/"],
        ["--co"],
    ),
)
def test_collection_filters_are_rejected(addopts: str | list[str]) -> None:
    with pytest.raises(AssertionError):
        _assert_default_collection_options(addopts)


def _assert_default_collection_options(options: str | list[str]) -> None:
    arguments = shlex.split(options) if isinstance(options, str) else options
    assert all(isinstance(argument, str) for argument in arguments)

    for argument in arguments:
        for collection_filter in PYTEST_COLLECTION_FILTERS:
            is_short_filter = (
                collection_filter in {"-k", "-m"}
                and not argument.startswith("--")
                and argument.startswith(collection_filter)
            )
            is_long_filter = (
                argument == collection_filter
                or argument.startswith(f"{collection_filter}=")
            )
            assert not is_short_filter
            assert not is_long_filter

    positional_targets = [
        argument for argument in arguments if not argument.startswith("-")
    ]
    assert positional_targets in ([], ["tests"], ["tests/"])


def _workflow_pytest_commands(workflow_path: Path) -> list[tuple[str, str]]:
    loaded = yaml.load(
        workflow_path.read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert isinstance(loaded, dict)
    jobs = loaded["jobs"]
    assert isinstance(jobs, dict)

    commands: list[tuple[str, str]] = []
    for job_id, job in jobs.items():
        assert isinstance(job, dict)
        steps = job.get("steps", [])
        assert isinstance(steps, list)
        for step in steps:
            if not isinstance(step, dict) or not isinstance(step.get("run"), str):
                continue
            commands.extend(
                (job_id, line.strip())
                for line in step["run"].splitlines()
                if line.strip().startswith("pytest ")
            )
    return commands


def _assert_workflow_pytest_policy(commands: list[tuple[str, str]]) -> None:
    """One unnarrowed full-suite run, plus only declared platform supplements."""
    full_suite = [
        command
        for job_id, command in commands
        if job_id not in PLATFORM_SUPPLEMENT_JOBS
    ]
    assert len(full_suite) == 1
    arguments = shlex.split(full_suite[0])
    assert arguments[0] == "pytest"
    _assert_default_collection_options(arguments[1:])

    for job_id, command in commands:
        allowed_target = PLATFORM_SUPPLEMENT_JOBS.get(job_id)
        if allowed_target is None:
            continue
        arguments = shlex.split(command)
        assert arguments[0] == "pytest"
        positional_targets = [
            argument for argument in arguments[1:] if not argument.startswith("-")
        ]
        assert positional_targets == [allowed_target]


@pytest.mark.parametrize(
    "commands",
    (
        # A second full-suite job hiding a narrowed run.
        [("build", "pytest tests/"), ("extra", "pytest tests/mining/")],
        # A declared supplement reaching outside its allowed target.
        [("build", "pytest tests/"), ("mining-macos", "pytest tests/adapters/")],
        # A declared supplement widening to the whole suite is still a
        # supplement, not the gate — the gate would then be missing.
        [("mining-macos", "pytest tests/")],
    ),
)
def test_undeclared_or_widened_narrowing_is_rejected(
    commands: list[tuple[str, str]],
) -> None:
    with pytest.raises(AssertionError):
        _assert_workflow_pytest_policy(commands)


def test_declared_platform_supplement_is_accepted() -> None:
    _assert_workflow_pytest_policy(
        [("build", "pytest tests/"), ("mining-macos", "pytest tests/mining/ -x")]
    )


def test_integration_marker_documents_default_ci_collection() -> None:
    """The marker and CI must agree that integration tests run by default."""
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    pytest_options = pyproject["tool"]["pytest"]["ini_options"]
    markers = pytest_options["markers"]
    integration_marker = next(
        marker for marker in markers if marker.startswith("integration:")
    )

    assert "included in default pytest and CI runs" in integration_marker
    assert pytest_options["testpaths"] == ["tests"]
    _assert_default_collection_options(pytest_options.get("addopts", ""))
    for workflow in CI_WORKFLOWS:
        _assert_workflow_pytest_policy(_workflow_pytest_commands(workflow))

    for policy_doc, required_claim in INTEGRATION_POLICY_DOCS.items():
        policy_text = " ".join(
            policy_doc.read_text(encoding="utf-8").split()
        )
        assert required_claim in policy_text
        for stale_claim in STALE_OPT_IN_CLAIMS:
            assert stale_claim not in policy_text
