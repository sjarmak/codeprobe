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


def _workflow_pytest_commands(workflow_path: Path) -> list[str]:
    loaded = yaml.load(
        workflow_path.read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert isinstance(loaded, dict)
    jobs = loaded["jobs"]
    assert isinstance(jobs, dict)

    commands: list[str] = []
    for job in jobs.values():
        assert isinstance(job, dict)
        steps = job.get("steps", [])
        assert isinstance(steps, list)
        for step in steps:
            if not isinstance(step, dict) or not isinstance(step.get("run"), str):
                continue
            commands.extend(
                line.strip()
                for line in step["run"].splitlines()
                if line.strip().startswith("pytest ")
            )
    return commands


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
        commands = _workflow_pytest_commands(workflow)
        assert len(commands) == 1
        command = shlex.split(commands[0])
        assert command[0] == "pytest"
        _assert_default_collection_options(command[1:])

    for policy_doc, required_claim in INTEGRATION_POLICY_DOCS.items():
        policy_text = " ".join(
            policy_doc.read_text(encoding="utf-8").split()
        )
        assert required_claim in policy_text
        for stale_claim in STALE_OPT_IN_CLAIMS:
            assert stale_claim not in policy_text
