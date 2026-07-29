"""Policy checks for the release-blocking real-agent enterprise journey."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "publish.yml"


def _jobs() -> dict[str, object]:
    workflow = yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)
    assert isinstance(workflow, dict)
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def _steps(job: object) -> list[dict[str, object]]:
    assert isinstance(job, dict)
    steps = job["steps"]
    assert isinstance(steps, list)
    assert all(isinstance(step, dict) for step in steps)
    return steps


def _run_text(job: object) -> str:
    return "\n".join(str(step.get("run", "")) for step in _steps(job))


def test_real_journey_is_release_blocking_and_stub_gate_remains() -> None:
    jobs = _jobs()
    real = jobs["e2e-enterprise"]
    stub = jobs["e2e-self-serve"]
    gate = jobs["gate"]
    publish = jobs["publish"]
    assert isinstance(real, dict)
    assert isinstance(gate, dict)
    assert isinstance(publish, dict)

    assert real["needs"] == ["test", "e2e-self-serve"]
    assert gate["needs"] == ["e2e-enterprise"]
    assert publish["needs"] == ["gate"]
    assert "self_serve_acceptance.py" in _run_text(stub)
    assert "enterprise_journey.py" in _run_text(real)


def test_real_journey_uses_a_built_wheel_bounded_budget_and_protected_secrets() -> None:
    real = _jobs()["e2e-enterprise"]
    assert isinstance(real, dict)
    assert real["environment"] == "release-real-agent"
    assert real["timeout-minutes"] == "45"
    assert real["permissions"] == {"contents": "read"}

    text = _run_text(real)
    assert "python -m build" in text
    assert "pip install -e" not in text
    assert "--wheel dist/codeprobe-*.whl" in text
    assert "--max-cost-usd \"$CODEPROBE_RELEASE_MAX_COST_USD\"" in text
    assert "--candidate-version \"${GITHUB_REF_NAME#v}\"" in text
    assert "--candidate-commit \"$GITHUB_SHA\"" in text

    job_text = str(real)
    assert "secrets.CODEPROBE_RELEASE_AGENT_CREDENTIAL" in job_text
    assert "vars.CODEPROBE_RELEASE_AGENT" in job_text
    assert "CODEPROBE_RELEASE_MAX_COST_USD" in job_text
    assert "CODEPROBE_RELEASE_AGENT_CREDENTIAL" not in real["env"]
    journey_step = next(
        step for step in _steps(real)
        if step.get("name") == "Clean-wheel real-agent enterprise journey"
    )
    assert journey_step["env"] == {
        "CODEPROBE_RELEASE_AGENT_CREDENTIAL": (
            "${{ secrets.CODEPROBE_RELEASE_AGENT_CREDENTIAL }}"
        )
    }


def test_real_journey_retains_candidate_wheel_and_machine_evidence() -> None:
    real_steps = _steps(_jobs()["e2e-enterprise"])
    uploads = [
        step for step in real_steps
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert {step["with"]["name"] for step in uploads} == {
        "release-dist",
        "enterprise-journey-evidence",
    }
    for upload in uploads:
        assert upload["with"]["if-no-files-found"] == "error"
        assert upload["with"]["retention-days"] == "30"


def test_gate_validates_downloaded_evidence_against_exact_candidate() -> None:
    gate = _jobs()["gate"]
    gate_text = _run_text(gate)
    gate_steps = str(_steps(gate))
    assert "actions/download-artifact@" in gate_steps
    assert "'path': 'candidate-dist/'" in gate_steps
    assert "scripts/enterprise_release_gate.py" in gate_text
    assert '--expected-version "${GITHUB_REF_NAME#v}"' in gate_text
    assert '--expected-commit "$GITHUB_SHA"' in gate_text
    assert '--expected-agent-image "$CODEPROBE_RELEASE_AGENT_IMAGE"' in gate_text
    assert '--expected-scoring-image "$CODEPROBE_RELEASE_SCORING_IMAGE"' in gate_text
    assert "--wheel candidate-dist/codeprobe-*.whl" in gate_text
    assert "--evidence enterprise-evidence/enterprise-journey.json" in gate_text
    assert "python -m build --sdist --outdir candidate-dist" in gate_text
    assert "twine check candidate-dist/*" in gate_text
    assert "scripts/check_release_artifacts.py candidate-dist/" in gate_text

    uploads = [
        step for step in _steps(gate)
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert len(uploads) == 1
    assert uploads[0]["with"]["path"] == "candidate-dist/"


def test_real_agent_credential_is_not_available_to_stub_gate_or_publish_gate() -> None:
    jobs = _jobs()
    protected_name = "CODEPROBE_RELEASE_AGENT_CREDENTIAL"
    assert protected_name in str(jobs["e2e-enterprise"])
    assert protected_name not in str(jobs["test"])
    assert protected_name not in str(jobs["e2e-self-serve"])
    assert protected_name not in str(jobs["gate"])
    assert protected_name not in str(jobs["publish"])
