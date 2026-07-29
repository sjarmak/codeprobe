# OCI Image Contract

CodeProbe publishes two execution images for each release:

- `codeprobe-agent` runs the agent subprocess with network access for model API calls.
- `codeprobe-scoring` runs mined test and verifier scripts with `--network=none`.

Release tags build both images for `linux/amd64` and `linux/arm64`. The workflow
pushes a unique candidate tag, scans both platform manifests, verifies
SBOM/provenance attestations, signs the digest with keyless Cosign, and only then
promotes the immutable package version tag. The promoted digest is recorded in
the `oci-image-identity-*` workflow artifacts.

## Runtime Reference Resolution

Runtime defaults use the installed `codeprobe` package version:

```text
codeprobe-agent:<installed-version>
codeprobe-scoring:<installed-version>
```

Set these environment variables to use a registry mirror or digest pin:

```bash
export CODEPROBE_IMAGE_REGISTRY=registry.example.test
export CODEPROBE_IMAGE_NAMESPACE=platform/codeprobe
export CODEPROBE_IMAGE_VERSION=0.13.0
```

Those variables compose:

```text
registry.example.test/platform/codeprobe/codeprobe-agent:0.13.0
registry.example.test/platform/codeprobe/codeprobe-scoring:0.13.0
```

Exact references win over the composed form:

```bash
export CODEPROBE_AGENT_IMAGE='registry.example.test/platform/codeprobe/codeprobe-agent@sha256:<agent-digest>'
export CODEPROBE_SCORING_IMAGE='registry.example.test/platform/codeprobe/codeprobe-scoring@sha256:<scoring-digest>'
```

CodeProbe never pulls images automatically. Operators make the chosen references
available to docker or podman before running `codeprobe run`.

Runtime image overrides must be explicit OCI references: either a non-`latest`
tag or a `sha256` digest pin. CodeProbe rejects URL-shaped values, whitespace,
empty values, uppercase repository components, malformed digests, and implicit
tag references before invoking docker or podman.

## Private-Registry Mirroring

Mirror the release tag and all platform manifests with a registry-aware copier:

```bash
skopeo copy --all --preserve-digests \
  docker://<source-registry>/<source-namespace>/codeprobe-agent:<version> \
  docker://<private-registry>/<private-namespace>/codeprobe-agent:<version>

skopeo copy --all --preserve-digests \
  docker://<source-registry>/<source-namespace>/codeprobe-scoring:<version> \
  docker://<private-registry>/<private-namespace>/codeprobe-scoring:<version>
```

After mirroring, inspect the private-registry digests and configure exact
runtime references. Copy OCI referrers with the same digest so signatures and
attestations remain available at the destination:

```bash
skopeo inspect --raw \
  docker://<private-registry>/<private-namespace>/codeprobe-agent:<version>

oras copy --recursive \
  <source-registry>/<source-namespace>/codeprobe-agent@sha256:<agent-digest> \
  <private-registry>/<private-namespace>/codeprobe-agent@sha256:<agent-digest>

oras copy --recursive \
  <source-registry>/<source-namespace>/codeprobe-scoring@sha256:<scoring-digest> \
  <private-registry>/<private-namespace>/codeprobe-scoring@sha256:<scoring-digest>

export CODEPROBE_AGENT_IMAGE='<private-registry>/<private-namespace>/codeprobe-agent@sha256:<agent-digest>'
export CODEPROBE_SCORING_IMAGE='<private-registry>/<private-namespace>/codeprobe-scoring@sha256:<scoring-digest>'
```

Re-verify trust at the private-registry digest, not at the source tag:

```bash
cosign verify \
  --certificate-identity '<release-workflow-identity>' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  '<private-registry>/<private-namespace>/codeprobe-agent@sha256:<agent-digest>'

gh attestation verify \
  'oci://<private-registry>/<private-namespace>/codeprobe-agent@sha256:<agent-digest>' \
  --repo '<source-owner>/<source-repo>' \
  --cert-identity '<release-workflow-identity>' \
  --cert-oidc-issuer 'https://token.actions.githubusercontent.com' \
  --bundle-from-oci
```

Repeat the verification for `codeprobe-scoring`. Keep the workflow's
`oci-image-identity-*` JSON artifacts with the release record so operators can
compare the source digest, mirrored digest, and runtime digest pin during change
review.

## Offline OCI Archive Transfer

For an airgapped registry, copy the multi-platform image into an OCI archive,
copy the recursive signature and attestation referrers into OCI layouts, move
those archives plus the offline trust material through the approved offline
channel, then import them into the destination registry:

```bash
skopeo copy --all --preserve-digests \
  docker://<source-registry>/<source-namespace>/codeprobe-agent:<version> \
  oci-archive:codeprobe-agent-<version>.tar

skopeo copy --all --preserve-digests \
  docker://<source-registry>/<source-namespace>/codeprobe-scoring:<version> \
  oci-archive:codeprobe-scoring-<version>.tar

oras copy --recursive --to-oci-layout \
  <source-registry>/<source-namespace>/codeprobe-agent@sha256:<agent-digest> \
  codeprobe-agent-referrers:<version>

oras copy --recursive --to-oci-layout \
  <source-registry>/<source-namespace>/codeprobe-scoring@sha256:<scoring-digest> \
  codeprobe-scoring-referrers:<version>

tar -cf codeprobe-agent-referrers-<version>.tar codeprobe-agent-referrers
tar -cf codeprobe-scoring-referrers-<version>.tar codeprobe-scoring-referrers

sha256sum \
  codeprobe-agent-<version>.tar \
  codeprobe-scoring-<version>.tar \
  codeprobe-agent-referrers-<version>.tar \
  codeprobe-scoring-referrers-<version>.tar

skopeo copy --all --preserve-digests \
  oci-archive:codeprobe-agent-<version>.tar \
  docker://<private-registry>/<private-namespace>/codeprobe-agent:<version>

skopeo copy --all --preserve-digests \
  oci-archive:codeprobe-scoring-<version>.tar \
  docker://<private-registry>/<private-namespace>/codeprobe-scoring:<version>

tar -xf codeprobe-agent-referrers-<version>.tar
tar -xf codeprobe-scoring-referrers-<version>.tar

oras copy --recursive --from-oci-layout \
  codeprobe-agent-referrers:<version> \
  <private-registry>/<private-namespace>/codeprobe-agent@sha256:<agent-digest>

oras copy --recursive --from-oci-layout \
  codeprobe-scoring-referrers:<version> \
  <private-registry>/<private-namespace>/codeprobe-scoring@sha256:<scoring-digest>
```

After import, inspect the destination digest and run the same `cosign verify`
and `gh attestation verify --bundle-from-oci` checks against the imported digest.
Carry the release workflow identity, expected source repository, OIDC issuer,
and your environment's approved Sigstore/GitHub verification roots as offline
trust material. Record the archive checksums, resulting private-registry
digests, verification inputs, and verification output in the change ticket or
release evidence store. Do not replace digest pins with mutable operator-local
tags after transfer.
