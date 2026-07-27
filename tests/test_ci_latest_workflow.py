"""Latest-dependency workflow policy tests."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci-latest.yml"


def _workflow() -> dict[str, object]:
    loaded = yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)
    assert isinstance(loaded, dict)
    return loaded


def test_latest_dependency_pytest_step_sets_explicit_tenant() -> None:
    jobs = _workflow()["jobs"]
    assert isinstance(jobs, dict)
    test_latest = jobs["test-latest"]
    assert isinstance(test_latest, dict)
    steps = test_latest["steps"]
    assert isinstance(steps, list)
    pytest_steps = [
        step
        for step in steps
        if isinstance(step, dict)
        and isinstance(step.get("run"), str)
        and step["run"].startswith("pytest ")
    ]

    assert len(pytest_steps) == 1
    assert pytest_steps[0]["env"] == {
        "CODEPROBE_TENANT": "ci-${{ github.sha }}",
    }
