"""OCI image documentation policy tests."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OCI_IMAGES_DOC_PATH = REPO_ROOT / "docs" / "oci_images.md"


def _doc() -> str:
    return OCI_IMAGES_DOC_PATH.read_text()


def _offline_section() -> str:
    return _doc().split("## Offline OCI Archive Transfer", 1)[1]


def test_docs_cover_mirroring_offline_transfer_and_runtime_config() -> None:
    doc = _doc()

    assert "CODEPROBE_AGENT_IMAGE" in doc
    assert "CODEPROBE_SCORING_IMAGE" in doc
    assert "CODEPROBE_IMAGE_REGISTRY" in doc
    assert "CODEPROBE_IMAGE_NAMESPACE" in doc
    assert "configuration-required" in doc
    assert "ghcr.io/sjarmak/codeprobe/codeprobe-agent" not in doc
    assert "docker.io/library/*" in doc
    assert "codeprobe-release-pair:<version>" in doc
    assert "skopeo copy --all" in doc
    assert "--preserve-digests" in doc
    assert "oci-archive:" in doc
    assert "oras copy --recursive" in doc
    assert "--to-oci-layout" in doc
    assert "--from-oci-layout" in doc
    assert "cosign verify" in doc
    assert "gh attestation verify" in doc
    assert "--bundle-from-oci" in doc


def test_docs_pin_offline_trust_command_shapes() -> None:
    offline = _offline_section()

    assert "gh attestation download" in offline
    assert "mkdir -p github-attestation-agent github-attestation-scoring" in offline
    assert "(cd github-attestation-agent &&" in offline
    assert "(cd github-attestation-scoring &&" in offline
    assert "--output-folder" not in offline
    assert "gh attestation trusted-root" in offline
    assert "--bundle github-attestation" in offline
    assert "--custom-trusted-root github-trusted-root.jsonl" in offline
    assert "--source-ref 'refs/tags/v<version>'" in offline
    assert "--source-digest '<release-sha>'" in offline
    assert "cosign initialize" in offline
    assert "cosign save" in offline
    assert "cosign verify --local-image" in offline
    assert "--trusted-root cosign-trusted-root.json" in offline
    assert "--bundle-from-oci" not in offline
    assert "--offline" not in offline


def test_docs_verify_offline_release_pair_authority() -> None:
    offline = _offline_section()

    assert "oras pull" in offline
    assert "cosign verify-blob" in offline
    assert "--bundle release-pair.bundle" in offline
    assert "--trusted-root cosign-trusted-root.json" in offline
    assert "--certificate-identity '<release-workflow-identity>'" in offline
    assert "--certificate-oidc-issuer 'https://token.actions.githubusercontent.com'" in offline
    assert "release-pair.json" in offline
    assert ".source.sha" in offline
    assert 'select(.image == "codeprobe-agent") | .digest' in offline
    assert 'select(.image == "codeprobe-scoring") | .digest' in offline
    assert 'test "$PAIR_AGENT_DIGEST" = "$AGENT_DEST_DIGEST"' in offline
    assert 'test "$PAIR_SCORING_DIGEST" = "$SCORING_DEST_DIGEST"' in offline


def test_docs_cover_release_authority_protections() -> None:
    doc = _doc()

    assert "protected `main` branch" in doc
    assert "protected `v*` tags" in doc
    assert "`release-images` environment" in doc
    assert "immutable version tags" in doc
    assert "immutable release-pair" in doc
