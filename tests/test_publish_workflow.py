"""Publication workflow policy tests."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "publish.yml"


def _workflow() -> dict[str, object]:
    loaded = yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)
    assert isinstance(loaded, dict)
    return loaded


def test_publish_job_requires_combined_release_gate() -> None:
    jobs = _workflow()["jobs"]
    assert isinstance(jobs, dict)
    gate = jobs["gate"]
    publish = jobs["publish"]
    assert isinstance(gate, dict)
    assert isinstance(publish, dict)
    assert publish["needs"] == ["gate"]

    gate_steps = gate["steps"]
    assert isinstance(gate_steps, list)
    gate_commands = [
        step["run"]
        for step in gate_steps
        if isinstance(step, dict) and "run" in step
    ]
    assert any(
        "scripts/release_gate.py" in command
        and "--evidence-dir acceptance/release-verdicts" in command
        and '--expected-version "${GITHUB_REF_NAME#v}"' in command
        for command in gate_commands
    )


def test_pypi_credentials_exist_only_after_gate_job() -> None:
    jobs = _workflow()["jobs"]
    assert isinstance(jobs, dict)
    gate_text = str(jobs["gate"])
    publish_text = str(jobs["publish"])

    assert "CODEPROBE" not in gate_text
    assert "twine upload" not in gate_text
    assert "CODEPROBE" in publish_text
    assert "twine upload" in publish_text
