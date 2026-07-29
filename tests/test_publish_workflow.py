"""Publication workflow policy tests."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "publish.yml"
IMAGE_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "publish-images.yml"
AGENT_DOCKERFILE_PATH = REPO_ROOT / "src" / "codeprobe" / "sandbox" / "Dockerfile.agent"
SCORING_DOCKERFILE_PATH = (
    REPO_ROOT / "src" / "codeprobe" / "sandbox" / "Dockerfile.scoring"
)
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"


def _workflow() -> dict[str, object]:
    loaded = yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)
    assert isinstance(loaded, dict)
    return loaded


def _image_workflow() -> dict[str, object]:
    loaded = yaml.load(IMAGE_WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)
    assert isinstance(loaded, dict)
    return loaded


def _candidate_image_steps() -> list[dict[str, object]]:
    jobs = _image_workflow()["jobs"]
    assert isinstance(jobs, dict)
    build = jobs["build-candidate-images"]
    assert isinstance(build, dict)
    steps = build["steps"]
    assert isinstance(steps, list)
    assert all(isinstance(step, dict) for step in steps)
    return steps


def _authorize_steps() -> list[dict[str, object]]:
    jobs = _image_workflow()["jobs"]
    assert isinstance(jobs, dict)
    authorize = jobs["authorize-release"]
    assert isinstance(authorize, dict)
    steps = authorize["steps"]
    assert isinstance(steps, list)
    assert all(isinstance(step, dict) for step in steps)
    return steps


def _promotion_steps() -> list[dict[str, object]]:
    jobs = _image_workflow()["jobs"]
    assert isinstance(jobs, dict)
    promote = jobs["promote-images"]
    assert isinstance(promote, dict)
    steps = promote["steps"]
    assert isinstance(steps, list)
    assert all(isinstance(step, dict) for step in steps)
    return steps


def _cleanup_steps() -> list[dict[str, object]]:
    jobs = _image_workflow()["jobs"]
    assert isinstance(jobs, dict)
    cleanup = jobs["cleanup-unpromoted-candidates"]
    assert isinstance(cleanup, dict)
    steps = cleanup["steps"]
    assert isinstance(steps, list)
    assert all(isinstance(step, dict) for step in steps)
    return steps


def _reuse_steps() -> list[dict[str, object]]:
    jobs = _image_workflow()["jobs"]
    assert isinstance(jobs, dict)
    reuse = jobs["reuse-release"]
    assert isinstance(reuse, dict)
    steps = reuse["steps"]
    assert isinstance(steps, list)
    assert all(isinstance(step, dict) for step in steps)
    return steps


def _step_index(steps: list[dict[str, object]], label: str) -> int:
    for index, step in enumerate(steps):
        if step.get("name") == label or step.get("uses") == label:
            return index
    raise AssertionError(f"missing workflow step {label!r}")


def _all_image_steps() -> list[dict[str, object]]:
    return (
        _authorize_steps()
        + _reuse_steps()
        + _candidate_image_steps()
        + _cleanup_steps()
        + _promotion_steps()
    )


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

    assert workflow["permissions"] == {}
    assert workflow["concurrency"] == {
        "group": "publish-oci-images-${{ github.ref_name }}",
        "cancel-in-progress": "false",
    }


def test_oci_image_workflow_authorizes_release_tag_from_main_environment() -> None:
    jobs = _image_workflow()["jobs"]
    assert isinstance(jobs, dict)
    authorize = jobs["authorize-release"]
    build = jobs["build-candidate-images"]
    assert isinstance(authorize, dict)
    assert isinstance(build, dict)
    assert authorize["environment"] == "release-images"
    assert authorize["permissions"] == {"contents": "read"}
    assert build["needs"] == ["authorize-release", "reuse-release"]
    assert build["if"] == "needs.reuse-release.outputs.reuse != 'true'"

    checkout = next(
        step
        for step in _authorize_steps()
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout["with"] == {"fetch-depth": "0", "persist-credentials": "false"}

    authorize_text = "\n".join(
        str(step["run"]) for step in _authorize_steps() if isinstance(step.get("run"), str)
    )
    assert "git fetch" not in authorize_text
    assert "refs/remotes/origin/main^{commit}" in authorize_text
    assert "TAG_COMMIT=$(git rev-parse \"${GITHUB_REF}^{commit}\")" in authorize_text
    assert "EVENT_COMMIT=$(git rev-parse \"${GITHUB_SHA}^{commit}\")" in authorize_text
    assert 'test "$TAG_COMMIT" = "$EVENT_COMMIT"' in authorize_text
    assert 'git merge-base --is-ancestor "$TAG_COMMIT" origin/main' in authorize_text
    assert "release_sha=" in authorize_text
    assert "TAG_COMMIT" in authorize_text
    assert "version={tag_version}" in authorize_text


def test_oci_image_workflow_reuses_verified_release_pair_before_rebuild() -> None:
    jobs = _image_workflow()["jobs"]
    assert isinstance(jobs, dict)
    reuse = jobs["reuse-release"]
    build = jobs["build-candidate-images"]
    promote = jobs["promote-images"]
    assert isinstance(reuse, dict)
    assert isinstance(build, dict)
    assert isinstance(promote, dict)

    assert reuse["needs"] == ["authorize-release"]
    assert reuse["permissions"] == {
        "contents": "read",
        "packages": "read",
        "attestations": "read",
    }
    assert reuse["outputs"] == {"reuse": "${{ steps.reuse.outputs.reuse }}"}
    assert build["if"] == "needs.reuse-release.outputs.reuse != 'true'"
    assert promote["if"] == "needs.reuse-release.outputs.reuse != 'true'"

    reuse_text = "\n".join(str(step.get("run", "")) for step in _reuse_steps())
    assert "python -m codeprobe.sandbox.oci_release check-reuse" in reuse_text
    assert "--registry \"$REGISTRY\"" in reuse_text
    assert "--namespace \"$IMAGE_NAMESPACE\"" in reuse_text
    assert "--source-sha \"${{ needs.authorize-release.outputs.release_sha }}\"" in reuse_text
    assert "--trivy-image \"$TRIVY_IMAGE\"" in reuse_text
    assert "--trivy-severity \"$TRIVY_SEVERITY\"" in reuse_text
    upload = next(
        step for step in _reuse_steps() if step.get("name") == "Upload release reuse evidence"
    )
    assert upload["if"] == "steps.reuse.outputs.reuse == 'true'"
    assert upload["with"]["path"] == "release-reuse/"


def test_oci_image_workflow_uses_job_scoped_permissions_and_no_checkout_credentials() -> None:
    jobs = _image_workflow()["jobs"]
    assert isinstance(jobs, dict)
    build = jobs["build-candidate-images"]
    reuse = jobs["reuse-release"]
    cleanup = jobs["cleanup-unpromoted-candidates"]
    promote = jobs["promote-images"]
    assert isinstance(build, dict)
    assert isinstance(reuse, dict)
    assert isinstance(cleanup, dict)
    assert isinstance(promote, dict)

    assert reuse["environment"] == "release-images"
    assert build["environment"] == "release-images"
    assert cleanup["environment"] == "release-images"
    assert promote["environment"] == "release-images"
    assert build["permissions"] == {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
        "attestations": "write",
        "artifact-metadata": "write",
    }
    assert promote["permissions"] == {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
    }
    assert cleanup["permissions"] == {"contents": "read", "packages": "write"}
    assert reuse["permissions"]["packages"] == "read"

    for job_name, job in jobs.items():
        if isinstance(job, dict) and job.get("permissions", {}).get("packages") == "write":
            assert job["environment"] == "release-images"
            assert "authorize-release" in job["needs"]

    for step in _candidate_image_steps():
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            with_config = step["with"]
            assert isinstance(with_config, dict)
            assert with_config["ref"] == "${{ needs.authorize-release.outputs.release_sha }}"
            assert with_config["persist-credentials"] == "false"


def test_oci_image_workflow_builds_both_images_for_required_platforms() -> None:
    jobs = _image_workflow()["jobs"]
    assert isinstance(jobs, dict)
    build = jobs["build-candidate-images"]
    assert isinstance(build, dict)
    strategy = build["strategy"]
    assert isinstance(strategy, dict)
    assert strategy["fail-fast"] == "false"
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
        for step in _candidate_image_steps()
        if str(step.get("uses", "")).startswith("docker/build-push-action@")
    ]
    assert len(build_steps) == 1
    with_config = build_steps[0]["with"]
    assert isinstance(with_config, dict)
    assert with_config["platforms"] == "linux/amd64,linux/arm64"
    assert with_config["push"] == "true"
    assert with_config["tags"] == "${{ steps.image.outputs.candidate_ref }}"
    assert with_config["sbom"] == "generator=${{ env.SCOUT_SBOM_INDEXER_IMAGE }}"
    assert with_config["provenance"] == "mode=max"


def test_oci_image_workflow_pins_actions_scanner_and_base_images() -> None:
    action_ref = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")
    uses = [
        str(step["uses"])
        for step in _all_image_steps()
        if isinstance(step.get("uses"), str)
    ]

    assert uses
    assert all(action_ref.fullmatch(use) for use in uses)
    assert {use: uses.count(use) for use in set(uses)} == _expected_action_counts()
    workflow_text = IMAGE_WORKFLOW_PATH.read_text()
    _assert_workflow_tool_pins(workflow_text)
    _assert_dockerfile_base_pins()


def _expected_action_counts() -> dict[str, int]:
    return {
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1": 5,
        "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97": 4,
        "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9": 4,
        "oras-project/setup-oras@1d808f7d7f6995cc68b7bf507bfe5c5446e1dc9d": 4,
        "sigstore/cosign-installer@6f9f17788090df1f26f669e9d70d6ae9567deba6": 3,
        "actions/attest@f7c74d28b9d84cb8768d0b8ca14a4bac6ef463e6": 1,
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a": 6,
        "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c": 1,
        "docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c": 3,
        "docker/setup-qemu-action@96fe6ef7f33517b61c61be40b68a1882f3264fb8": 1,
        "docker/login-action@371161bbe7024a29a25c5e19bfcbc0804fe9ad2c": 4,
        "docker/build-push-action@53b7df96c91f9c12dcc8a07bcb9ccacbed38856a": 1,
    }


def _assert_workflow_tool_pins(workflow_text: str) -> None:
    assert workflow_text.count("# actions/checkout v7.0.1") == 5
    assert workflow_text.count("# actions/setup-python v7.0.0") == 4
    assert workflow_text.count("# astral-sh/setup-uv v9.0.0") == 4
    assert workflow_text.count("# oras-project/setup-oras v2.0.1") == 4
    assert workflow_text.count("# sigstore/cosign-installer v4.1.2") == 3
    assert workflow_text.count("# actions/upload-artifact v7.0.1") == 6
    assert "# actions/attest v4.2.0" in workflow_text
    assert "# actions/download-artifact v8.0.1" in workflow_text
    assert "cosign-release: v3.1.2" in workflow_text
    assert "cosign-release: v3.0.2" not in workflow_text
    assert "ORAS_VERSION: 1.3.3" in workflow_text
    assert "ORAS_VERSION: 1.3.1" not in workflow_text
    assert "UV_VERSION: 0.12.0" in workflow_text
    assert (
        "TRIVY_IMAGE: ghcr.io/aquasecurity/trivy:0.72.0@sha256:"
        "cffe3f5161a47a6823fbd23d985795b3ed72a4c806da4c4df16266c02accdd6f"
    ) in workflow_text
    assert "QEMU_BINFMT_IMAGE: tonistiigi/binfmt@sha256:" in workflow_text
    assert "BUILDX_VERSION: v0.35.0" in workflow_text
    assert (
        "BUILDKIT_IMAGE: moby/buildkit:v0.31.2@sha256:"
        "2f5adac4ecd194d9f8c10b7b5d7bceb5186853db1b26e5abd3a657af0b7e26ec"
    ) in workflow_text
    assert "moby/buildkit:v0.25.1" not in workflow_text
    assert "moby/buildkit:v0.28.0" not in workflow_text
    assert "SCOUT_SBOM_INDEXER_IMAGE: docker/scout-sbom-indexer:1@sha256:" in workflow_text
    assert "DEBIAN_SNAPSHOT:" in workflow_text
    assert "CLAUDE_CODE_VERSION: " in workflow_text
    assert "CLAUDE_CODE_INTEGRITY: sha512-" in workflow_text


def _assert_dockerfile_base_pins() -> None:
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

    agent = AGENT_DOCKERFILE_PATH.read_text()
    scoring = SCORING_DOCKERFILE_PATH.read_text()
    for dockerfile in (agent, scoring):
        assert "ARG DEBIAN_SNAPSHOT=" in dockerfile
        assert "snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}" in dockerfile
        assert "snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}" in dockerfile
        assert re.search(r"^USER codeprobe$", dockerfile, re.MULTILINE)
    assert "ARG CLAUDE_CODE_VERSION=" in agent
    assert "ARG CLAUDE_CODE_INTEGRITY=sha512-" in agent
    assert "npm pack \"@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}\" --json" in agent
    assert "actual !== process.env.CLAUDE_CODE_INTEGRITY" in agent
    assert 'npm install -g "./${package_file}"' in agent


def test_oci_image_workflow_has_supply_chain_gates() -> None:
    steps = _all_image_steps()
    uses = {str(step.get("uses")).split("@", 1)[0] for step in steps if "uses" in step}
    run_text = "\n".join(
        str(step["run"]) for step in steps if isinstance(step.get("run"), str)
    )
    names = {str(step.get("name")) for step in steps}

    _assert_supply_chain_actions_present(uses)
    _assert_setup_step_inputs(steps)
    _assert_supply_chain_step_names(names)
    _assert_supply_chain_run_commands(run_text)


def _assert_supply_chain_actions_present(uses: set[str]) -> None:
    assert "actions/setup-python" in uses
    assert "astral-sh/setup-uv" in uses
    assert "docker/setup-qemu-action" in uses
    assert "docker/setup-buildx-action" in uses
    assert "docker/login-action" in uses
    assert "actions/attest" in uses
    assert "sigstore/cosign-installer" in uses
    assert "actions/download-artifact" in uses
    assert "oras-project/setup-oras" in uses


def _assert_setup_step_inputs(steps: list[dict[str, object]]) -> None:
    setup_python = next(
        step for step in steps
        if str(step.get("uses", "")).startswith("actions/setup-python@")
    )
    assert setup_python["with"] == {"python-version": "3.11"}
    setup_uv = next(
        step for step in steps
        if str(step.get("uses", "")).startswith("astral-sh/setup-uv@")
    )
    assert setup_uv["with"] == {"version": "${{ env.UV_VERSION }}"}
    setup_qemu = next(
        step for step in steps
        if str(step.get("uses", "")).startswith("docker/setup-qemu-action@")
    )
    assert setup_qemu["with"] == {
        "image": "${{ env.QEMU_BINFMT_IMAGE }}",
        "platforms": "arm64",
    }
    setup_buildx = next(
        step for step in steps
        if str(step.get("uses", "")).startswith("docker/setup-buildx-action@")
    )
    assert setup_buildx["with"] == {
        "version": "${{ env.BUILDX_VERSION }}",
        "driver-opts": "image=${{ env.BUILDKIT_IMAGE }}",
    }


def _assert_supply_chain_step_names(names: set[str]) -> None:
    assert "Resolve registry credentials" in names
    assert "Vulnerability policy scan for each platform" in names
    assert "Verify BuildKit SBOM and provenance attestations" in names
    assert "Verify GitHub provenance attestation" in names
    assert "Promote pair-level immutable version tags" in names


def _assert_supply_chain_run_commands(run_text: str) -> None:
    assert "pyproject.toml version" in run_text
    assert "CODEPROBE_OCI_USERNAME and CODEPROBE_OCI_TOKEN are required" not in run_text
    assert "--exit-code 1" in run_text
    assert "--severity \"$TRIVY_SEVERITY\"" in run_text
    assert "for platform in linux/amd64 linux/arm64" in run_text
    assert "--platform \"$platform\"" in run_text
    assert "--cap-drop=ALL" in run_text
    assert "--security-opt=no-new-privileges" in run_text
    assert "--pids-limit=256" in run_text
    assert "--memory=4g" in run_text
    assert "--memory-swap=4g" in run_text
    assert "--cpus=2" in run_text
    assert "--read-only" in run_text
    assert "--tmpfs /tmp:rw,noexec,nosuid,nodev,size=256m" in run_text
    assert "--tmpfs /root/.cache:rw,nosuid,nodev,size=2g" in run_text
    assert "python -m codeprobe.sandbox.oci_attestations" in run_text
    assert "python -m codeprobe.sandbox.oci_release check-reuse" in run_text
    assert "python -m codeprobe.sandbox.oci_release promote-tags" in run_text
    assert "python -m codeprobe.sandbox.oci_release publish-pair" in run_text
    assert "python -m codeprobe.sandbox.oci_workflow_inputs credentials" in run_text
    assert "python -m codeprobe.sandbox.oci_workflow_inputs image-refs" in run_text
    assert "uv sync --frozen --no-dev" in run_text
    assert "uv run --frozen --no-dev python -m" in run_text
    assert "--image-ref \"$IMAGE_REF\"" in run_text
    assert "--candidate-ref \"$CANDIDATE_REF\"" in run_text
    assert "--digest-ref \"$DIGEST_REF\"" in run_text
    assert "gh attestation verify \"oci://${DIGEST_REF}\"" in run_text
    assert "cosign sign --yes \"$DIGEST_REF\"" in run_text
    assert "cosign verify" in run_text
    assert "oci-identities" in run_text


def test_oci_image_helper_jobs_checkout_and_sync_before_helpers() -> None:
    jobs = _image_workflow()["jobs"]
    assert isinstance(jobs, dict)
    helper_jobs = {
        "reuse-release": _reuse_steps(),
        "build-candidate-images": _candidate_image_steps(),
        "cleanup-unpromoted-candidates": _cleanup_steps(),
        "promote-images": _promotion_steps(),
    }

    for job_name, steps in helper_jobs.items():
        checkout = next(
            step
            for step in steps
            if str(step.get("uses", "")).startswith("actions/checkout@")
        )
        assert checkout["with"] == {
            "ref": "${{ needs.authorize-release.outputs.release_sha }}",
            "persist-credentials": "false",
        }
        sync_index = _step_index(steps, "Install locked Python dependencies")
        helper_indices = [
            index
            for index, step in enumerate(steps)
            if "python -m codeprobe.sandbox." in str(step.get("run", ""))
        ]
        assert helper_indices, job_name
        assert sync_index < min(helper_indices)
        for index in helper_indices:
            assert "uv run --frozen --no-dev python -m" in str(steps[index]["run"])


def test_oci_image_workflow_validates_credentials_before_outputs() -> None:
    credential_steps = [
        step
        for step in (_reuse_steps() + _candidate_image_steps() + _cleanup_steps() + _promotion_steps())
        if step.get("name") == "Resolve registry credentials"
    ]
    assert len(credential_steps) == 4
    for step in credential_steps:
        run_text = str(step["run"])
        assert "codeprobe.sandbox.oci_workflow_inputs credentials" in run_text
        assert "--registry \"$REGISTRY\"" in run_text
        assert "--namespace \"$IMAGE_NAMESPACE\"" in run_text
        assert "--github-output \"$GITHUB_OUTPUT\"" in run_text
        assert "::add-mask::" not in run_text
        assert "username=" not in run_text
        assert "password=" not in run_text


def test_oci_image_workflow_promotes_version_tag_only_after_gates() -> None:
    jobs = _image_workflow()["jobs"]
    assert isinstance(jobs, dict)
    promote_job = jobs["promote-images"]
    assert isinstance(promote_job, dict)
    _assert_promote_job_shape(promote_job)
    _assert_candidate_build_uses_only_candidate_tags()
    _assert_promotion_order_and_quarantine()
    _assert_candidate_gates_present()


def _assert_promote_job_shape(promote_job: dict[str, object]) -> None:
    assert promote_job["needs"] == [
        "build-candidate-images",
        "authorize-release",
        "reuse-release",
    ]
    assert promote_job["if"] == "needs.reuse-release.outputs.reuse != 'true'"
    assert "strategy" not in promote_job


def _assert_candidate_build_uses_only_candidate_tags() -> None:
    steps = _candidate_image_steps()
    build = next(
        step
        for step in steps
        if str(step.get("uses", "")).startswith("docker/build-push-action@")
    )
    build_with = build["with"]
    assert isinstance(build_with, dict)
    assert "version_ref" not in str(build_with)
    assert build_with["tags"] == "${{ steps.image.outputs.candidate_ref }}"


def _assert_promotion_order_and_quarantine() -> None:
    promote_steps = _promotion_steps()
    promote_index = _step_index(promote_steps, "Promote pair-level immutable version tags")
    pair_index = _step_index(
        promote_steps, "Create, sign, and publish release pair authority"
    )
    evidence_index = _step_index(promote_steps, "Upload release pair evidence")
    assert promote_index < pair_index < evidence_index
    promote_text = "\n".join(str(step.get("run", "")) for step in promote_steps)
    pair_step = next(
        step
        for step in promote_steps
        if step.get("name") == "Create, sign, and publish release pair authority"
    )
    quarantine_upload = next(
        step
        for step in promote_steps
        if step.get("name") == "Upload promotion quarantine evidence"
    )
    quarantine_step = next(
        step
        for step in promote_steps
        if step.get("name") == "Quarantine partial promotion on failure"
    )
    pair_text = str(pair_step["run"])
    quarantine_path = str(quarantine_upload["with"]["path"])
    assert "python -m codeprobe.sandbox.oci_release promote-tags" in promote_text
    assert "python -m codeprobe.sandbox.oci_release publish-pair" in promote_text
    assert "python -m codeprobe.sandbox.oci_release quarantine-promotion" in promote_text
    assert "--registry \"$REGISTRY\"" in promote_text
    assert "--namespace \"$IMAGE_NAMESPACE\"" in promote_text
    assert "--version \"${{ needs.authorize-release.outputs.version }}\"" in promote_text
    assert "--source-sha \"${{ needs.authorize-release.outputs.release_sha }}\"" in promote_text
    assert "--output-dir release-pair" in promote_text
    assert "release-pair/release-pair.json" in str(_promotion_steps())
    assert "Quarantine partial promotion on failure" in str(promote_steps)
    assert "promotion-state.json" in promote_text
    assert "release-pair/release-pair-ref.json" in str(_promotion_steps())
    assert "GITHUB_RUN_ID" not in pair_text
    assert "GITHUB_RUN_ATTEMPT" not in pair_text
    assert "release-pair.bundle" not in quarantine_path
    assert quarantine_step["if"] == (
        "(failure() || cancelled()) && hashFiles('promotion-state.json') != ''"
    )
    assert quarantine_upload["if"] == (
        "always() && hashFiles('promotion-quarantine.json') != ''"
    )
    assert quarantine_upload["with"]["if-no-files-found"] == "error"


def _assert_candidate_gates_present() -> None:
    steps = _candidate_image_steps()
    for gate in (
        "Vulnerability policy scan for each platform",
        "Verify BuildKit SBOM and provenance attestations",
        "Verify GitHub provenance attestation",
        "Keyless sign digest",
        "Verify keyless signature",
    ):
        assert _step_index(steps, gate) < len(steps)


def test_oci_image_workflow_quarantines_failed_candidates_after_post_push_gates() -> None:
    steps = _candidate_image_steps()
    cleanup_index = _step_index(steps, "Quarantine failed candidate tag")
    quarantine_upload_index = _step_index(
        steps, "Upload candidate cleanup quarantine evidence"
    )
    cleanup_step = steps[cleanup_index]
    quarantine_upload = steps[quarantine_upload_index]

    _assert_candidate_cleanup_conditions(cleanup_step, quarantine_upload)
    _assert_candidate_cleanup_order(steps, cleanup_index, quarantine_upload_index)
    cleanup_text = str(cleanup_step["run"])
    assert "python -m codeprobe.sandbox.oci_candidate_cleanup" in cleanup_text
    assert "--candidate-ref \"$CANDIDATE_REF\"" in cleanup_text
    assert "--build-digest \"$BUILD_DIGEST\"" in cleanup_text


def _assert_candidate_cleanup_conditions(
    cleanup_step: dict[str, object], quarantine_upload: dict[str, object]
) -> None:
    assert cleanup_step["if"] == (
        "(failure() || cancelled()) && steps.image.outputs.candidate_ref != ''"
    )
    assert quarantine_upload["if"] == (
        "always() && hashFiles('candidate-quarantine/candidate-quarantine.json') != ''"
    )
    assert quarantine_upload["with"]["path"] == "candidate-quarantine/"
    assert quarantine_upload["with"]["if-no-files-found"] == "error"


def _assert_candidate_cleanup_order(
    steps: list[dict[str, object]], cleanup_index: int, quarantine_upload_index: int
) -> None:
    build_index = next(
        index
        for index, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("docker/build-push-action@")
    )
    identity_upload_index = next(
        index
        for index, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
        and "oci-identities" in str(step.get("with", {}).get("path", ""))
    )
    oras_index = next(
        index
        for index, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("oras-project/setup-oras@")
    )
    assert oras_index < build_index
    for gate in (
        "Capture candidate digest identity",
        "Vulnerability policy scan for each platform",
        "Verify BuildKit SBOM and provenance attestations",
        "Verify GitHub provenance attestation",
        "Keyless sign digest",
        "Verify keyless signature",
        "Record digest identity",
    ):
        assert build_index < _step_index(steps, gate) < cleanup_index
    assert build_index < identity_upload_index < cleanup_index < quarantine_upload_index


def test_oci_image_workflow_aggregates_candidate_cleanup_on_matrix_failure() -> None:
    jobs = _image_workflow()["jobs"]
    assert isinstance(jobs, dict)
    cleanup = jobs["cleanup-unpromoted-candidates"]
    assert isinstance(cleanup, dict)
    assert cleanup["needs"] == [
        "authorize-release",
        "reuse-release",
        "build-candidate-images",
    ]
    assert cleanup["if"] == (
        "always() && needs.authorize-release.result == 'success' && "
        "needs.reuse-release.result == 'success' && "
        "needs.reuse-release.outputs.reuse != 'true' && "
        "needs.build-candidate-images.result != 'success'"
    )

    steps = _cleanup_steps()
    cleanup_step = next(
        step
        for step in steps
        if step.get("name") == "Quarantine unpromoted run candidates"
    )
    upload = next(
        step
        for step in steps
        if step.get("name") == "Upload aggregate candidate quarantine evidence"
    )
    cleanup_text = str(cleanup_step["run"])
    assert "for image in codeprobe-agent codeprobe-scoring" in cleanup_text
    assert "uv run --frozen --no-dev python -m codeprobe.sandbox.oci_candidate_cleanup" in cleanup_text
    assert '--build-digest ""' in cleanup_text
    assert '--digest-ref ""' in cleanup_text
    assert upload["if"] == (
        "always() && hashFiles('candidate-quarantine/**/candidate-quarantine.json') != ''"
    )
    assert upload["with"]["if-no-files-found"] == "error"


def test_oci_image_workflow_records_version_and_digest_without_latest_tags() -> None:
    workflow_text = IMAGE_WORKFLOW_PATH.read_text()

    assert ":latest" not in workflow_text
    assert "value=latest" not in workflow_text
    assert "steps.version.outputs.version" not in workflow_text
    assert "steps.build.outputs.digest" in workflow_text
    assert '"digest_ref"' in workflow_text
    assert '"platforms": ["linux/amd64", "linux/arm64"]' in workflow_text
    assert "candidate_ref" in workflow_text
    assert "version_ref" in workflow_text
    assert "release_pair_schema" not in workflow_text


def test_runtime_parser_dependency_is_declared() -> None:
    pyproject = PYPROJECT_PATH.read_text()

    assert '"docker-image-py>=0.2,<0.3"' in pyproject
