"""Publication workflow policy tests."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "publish.yml"
IMAGE_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "publish-images.yml"
OCI_IMAGES_DOC_PATH = REPO_ROOT / "docs" / "oci_images.md"
AGENT_DOCKERFILE_PATH = REPO_ROOT / "src" / "codeprobe" / "sandbox" / "Dockerfile.agent"
SCORING_DOCKERFILE_PATH = (
    REPO_ROOT / "src" / "codeprobe" / "sandbox" / "Dockerfile.scoring"
)


def _workflow() -> dict[str, object]:
    loaded = yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)
    assert isinstance(loaded, dict)
    return loaded


def _image_workflow() -> dict[str, object]:
    loaded = yaml.load(IMAGE_WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)
    assert isinstance(loaded, dict)
    return loaded


def _publish_image_steps() -> list[dict[str, object]]:
    jobs = _image_workflow()["jobs"]
    assert isinstance(jobs, dict)
    publish = jobs["publish-images"]
    assert isinstance(publish, dict)
    steps = publish["steps"]
    assert isinstance(steps, list)
    assert all(isinstance(step, dict) for step in steps)
    return steps


def _step_index(steps: list[dict[str, object]], label: str) -> int:
    for index, step in enumerate(steps):
        if step.get("name") == label or step.get("uses") == label:
            return index
    raise AssertionError(f"missing workflow step {label!r}")


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


def test_oci_image_workflow_is_tag_triggered_with_publish_permissions() -> None:
    workflow = _image_workflow()

    on = workflow["on"]
    assert isinstance(on, dict)
    push = on["push"]
    assert isinstance(push, dict)
    assert push["tags"] == ["v*"]

    assert workflow["permissions"] == {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
        "attestations": "write",
        "artifact-metadata": "write",
    }


def test_oci_image_workflow_builds_both_images_for_required_platforms() -> None:
    jobs = _image_workflow()["jobs"]
    assert isinstance(jobs, dict)
    publish = jobs["publish-images"]
    assert isinstance(publish, dict)
    strategy = publish["strategy"]
    assert isinstance(strategy, dict)
    matrix = strategy["matrix"]
    assert isinstance(matrix, dict)
    include = matrix["include"]
    assert include == [
        {
            "image": "codeprobe-agent",
            "dockerfile": "src/codeprobe/sandbox/Dockerfile.agent",
            "override_env": "CODEPROBE_AGENT_IMAGE",
        },
        {
            "image": "codeprobe-scoring",
            "dockerfile": "src/codeprobe/sandbox/Dockerfile.scoring",
            "override_env": "CODEPROBE_SCORING_IMAGE",
        },
    ]

    build_steps = [
        step
        for step in _publish_image_steps()
        if str(step.get("uses", "")).startswith("docker/build-push-action@")
    ]
    assert len(build_steps) == 1
    with_config = build_steps[0]["with"]
    assert isinstance(with_config, dict)
    assert with_config["platforms"] == "linux/amd64,linux/arm64"
    assert with_config["push"] == "true"
    assert with_config["tags"] == "${{ steps.image.outputs.candidate_ref }}"
    assert with_config["sbom"] == "true"
    assert with_config["provenance"] == "mode=max"


def test_oci_image_workflow_pins_actions_scanner_and_base_images() -> None:
    action_ref = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")
    uses = [
        str(step["uses"])
        for step in _publish_image_steps()
        if isinstance(step.get("uses"), str)
    ]

    assert uses
    assert all(action_ref.fullmatch(use) for use in uses)

    workflow_text = IMAGE_WORKFLOW_PATH.read_text()
    assert "TRIVY_IMAGE: ghcr.io/aquasecurity/trivy@sha256:" in workflow_text

    assert re.search(
        r"^FROM node:22-bookworm-slim@sha256:[0-9a-f]{64}$",
        AGENT_DOCKERFILE_PATH.read_text(),
        re.MULTILINE,
    )
    assert re.search(
        r"^FROM debian:bookworm-slim@sha256:[0-9a-f]{64}$",
        SCORING_DOCKERFILE_PATH.read_text(),
        re.MULTILINE,
    )


def test_oci_image_workflow_has_supply_chain_gates() -> None:
    steps = _publish_image_steps()
    uses = {str(step.get("uses")).split("@", 1)[0] for step in steps if "uses" in step}

    assert "actions/setup-python" in uses
    assert "docker/setup-qemu-action" in uses
    assert "docker/setup-buildx-action" in uses
    assert "docker/login-action" in uses
    assert "actions/attest" in uses
    assert "sigstore/cosign-installer" in uses

    run_text = "\n".join(
        str(step["run"]) for step in steps if isinstance(step.get("run"), str)
    )
    names = {str(step.get("name")) for step in steps}
    setup_python = next(step for step in steps if str(step.get("uses", "")).startswith("actions/setup-python@"))
    assert setup_python["with"] == {"python-version": "3.11"}

    assert "Resolve registry credentials" in names
    assert "Refuse existing immutable version tag" in names
    assert "Vulnerability policy scan for each platform" in names
    assert "Verify BuildKit SBOM and provenance attestations" in names
    assert "Verify GitHub provenance attestation" in names
    assert "Promote immutable version tag" in names

    assert "pyproject.toml version" in run_text
    assert "CODEPROBE_OCI_USERNAME and CODEPROBE_OCI_TOKEN are required" in run_text
    assert "version image tag already exists" in run_text
    assert "--exit-code 1" in run_text
    assert "--severity \"$TRIVY_SEVERITY\"" in run_text
    assert "for platform in linux/amd64 linux/arm64" in run_text
    assert "--platform \"$platform\"" in run_text
    assert "https://spdx.dev/Document" in run_text
    assert "https://slsa.dev/provenance/" in run_text
    assert "vnd.docker.reference.digest" in run_text
    assert "gh attestation verify \"oci://${DIGEST_REF}\"" in run_text
    assert "cosign sign --yes \"$DIGEST_REF\"" in run_text
    assert "cosign verify" in run_text
    assert "docker buildx imagetools create --tag \"$VERSION_REF\" \"$DIGEST_REF\"" in run_text
    assert "oci-identities" in run_text


def test_oci_image_workflow_promotes_version_tag_only_after_gates() -> None:
    steps = _publish_image_steps()
    build = next(
        step
        for step in steps
        if str(step.get("uses", "")).startswith("docker/build-push-action@")
    )
    build_with = build["with"]
    assert isinstance(build_with, dict)
    assert "version_ref" not in str(build_with)
    assert build_with["tags"] == "${{ steps.image.outputs.candidate_ref }}"

    promote_index = _step_index(steps, "Promote immutable version tag")
    for gate in (
        "Vulnerability policy scan for each platform",
        "Verify BuildKit SBOM and provenance attestations",
        "Verify GitHub provenance attestation",
        "Keyless sign digest",
        "Verify keyless signature",
    ):
        assert _step_index(steps, gate) < promote_index


def test_oci_image_workflow_records_version_and_digest_without_latest_tags() -> None:
    workflow_text = IMAGE_WORKFLOW_PATH.read_text()

    assert ":latest" not in workflow_text
    assert "value=latest" not in workflow_text
    assert "steps.version.outputs.version" in workflow_text
    assert "steps.build.outputs.digest" in workflow_text
    assert '"digest_ref"' in workflow_text
    assert '"platforms": ["linux/amd64", "linux/arm64"]' in workflow_text
    assert "candidate_ref" in workflow_text
    assert "version_ref" in workflow_text


def test_oci_image_docs_cover_mirroring_offline_transfer_and_runtime_config() -> None:
    doc = OCI_IMAGES_DOC_PATH.read_text()

    assert "CODEPROBE_AGENT_IMAGE" in doc
    assert "CODEPROBE_SCORING_IMAGE" in doc
    assert "CODEPROBE_IMAGE_REGISTRY" in doc
    assert "CODEPROBE_IMAGE_NAMESPACE" in doc
    assert "skopeo copy --all" in doc
    assert "oci-archive:" in doc
    assert "oras copy --recursive" in doc
    assert "cosign verify" in doc
    assert "gh attestation verify" in doc
    assert "--bundle-from-oci" in doc
