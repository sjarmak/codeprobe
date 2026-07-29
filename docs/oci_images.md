# OCI Image Contract

CodeProbe publishes two execution images for each release:

- `codeprobe-agent` runs the agent subprocess with network access for model API calls.
- `codeprobe-scoring` runs mined test and verifier scripts with `--network=none`.

Release tags build both images for `linux/amd64` and `linux/arm64`. The workflow
pushes a unique candidate tag, scans both platform manifests, verifies
SBOM/provenance payloads, signs the digest with keyless Cosign, and only then
promotes the immutable package version tag. After both official tags are written
and verified, the workflow signs a deterministic release-pair manifest and
publishes it as `codeprobe-release-pair:<version>` in the same registry namespace.
The promoted digests are recorded in the `oci-image-identity-*` artifacts; the
pair ref and digest are recorded in `oci-release-pair-*`.

The authority path relies on a protected `main` branch, protected `v*` tags,
and the `release-images` environment gate before package-write credentials are
available. Treat the published image version tags and the
`codeprobe-release-pair:<version>` record as immutable version tags and an
immutable release-pair authority; a rerun verifies and reuses an existing
trusted pair instead of overwriting it.

Failed run-unique candidate tags are quarantined, not deleted automatically.
OCI registry deletion is digest-addressed and has no provider-neutral atomic
"delete this tag only if it still identifies this digest" operation. Deleting
after a tag scan could therefore remove a manifest that another tag began
sharing concurrently. The workflow uploads `oci-candidate-quarantine-*`
evidence and fails closed; operators apply their registry retention policy to
those untrusted run-unique tags after confirming they are not shared.

## Runtime Reference Resolution

Runtime image resolution is configuration-required. CodeProbe does not embed a
project registry namespace in application code, and it will not fall back to an
unqualified `codeprobe-agent:<installed-version>` or
`codeprobe-scoring:<installed-version>` reference that Docker might resolve as
`docker.io/library/*`.

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

`CODEPROBE_IMAGE_REGISTRY` and `CODEPROBE_IMAGE_NAMESPACE` are a pair: if no
per-image override is set, both must be present and structurally valid. The
registry must be a qualified host such as `localhost`, `registry.example.test`,
`registry.example.test:5000`, or `[2001:db8::1]:5000`; the namespace must be a
valid OCI repository path.

Exact references win over the composed form:

```bash
export CODEPROBE_AGENT_IMAGE='registry.example.test/platform/codeprobe/codeprobe-agent@sha256:<agent-digest>'
export CODEPROBE_SCORING_IMAGE='registry.example.test/platform/codeprobe/codeprobe-scoring@sha256:<scoring-digest>'
```

Normal evaluation commands never pull images automatically. Operators run the
explicit `codeprobe bootstrap` trust-boundary command before `codeprobe run`.

Runtime image overrides must be explicit OCI references: either a non-`latest`
tag or a `sha256` digest pin. CodeProbe rejects URL-shaped values, whitespace,
empty values, uppercase repository components, malformed digests, and implicit
tag references before invoking docker or podman.

## Installed-Wheel Bootstrap

From any working directory, use one command to prepare both images for Docker
or Podman. No CodeProbe source checkout or Dockerfiles are required:

```bash
codeprobe bootstrap \
  --engine docker \
  --agent-image 'registry.example.test/platform/codeprobe/codeprobe-agent@sha256:<agent-digest>' \
  --scoring-image 'registry.example.test/platform/codeprobe/codeprobe-scoring@sha256:<scoring-digest>'
```

Omit `--engine` to select the first available Docker or Podman engine. A
tag-based `--agent-image` or `--scoring-image` is accepted only with its
corresponding `--agent-digest` or `--scoring-digest`.

Bootstrap pulls each digest-pinned reference, confirms the engine-reported
digest, captures the immutable local image ID, and writes both identities only
after both images pass verification. It fails closed on a missing tool,
malformed reference, digest mismatch, invalid engine response, timeout, or
partial preparation. The default record is
`~/.codeprobe/container-images.json`; set `CODEPROBE_CONTAINER_CONFIG` to an
absolute path to place it elsewhere. The record is written atomically with
mode `0600` and contains image identities, not registry credentials.

For a private registry, authenticate the selected engine before bootstrap.
The Docker, Podman, and Skopeo subprocesses inherit the operator environment,
including proxy variables and private CA variables. Configure the container
engine daemon or client trust store for the private CA as required by that
engine. CodeProbe does not copy authentication tokens or CA material into its
prepared-image record.

In an air-gapped environment, transfer verified OCI archives and import both
with Skopeo:

```bash
codeprobe bootstrap \
  --engine podman \
  --agent-image 'registry.example.test/platform/codeprobe/codeprobe-agent@sha256:<agent-digest>' \
  --scoring-image 'registry.example.test/platform/codeprobe/codeprobe-scoring@sha256:<scoring-digest>' \
  --agent-archive /transfer/codeprobe-agent.oci.tar \
  --scoring-archive /transfer/codeprobe-scoring.oci.tar
```

Offline preparation requires both archives. Bootstrap checks each OCI archive
digest before Skopeo copies it into the selected engine, then records the
engine's immutable local IDs. A missing archive, missing Skopeo binary, or
digest mismatch leaves the existing prepared-image record unchanged.

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

oras copy \
  <source-registry>/<source-namespace>/codeprobe-release-pair:<version> \
  <private-registry>/<private-namespace>/codeprobe-release-pair:<version>

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

Pull and verify the signed release pair before using the image digests:

```bash
oras pull \
  <private-registry>/<private-namespace>/codeprobe-release-pair:<version>

cosign verify-blob \
  --bundle release-pair.bundle \
  --certificate-identity '<release-workflow-identity>' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  release-pair.json
```

Repeat the image verification for `codeprobe-scoring`. Keep the workflow's
`oci-image-identity-*` JSON artifacts and the signed
`codeprobe-release-pair:<version>` artifact with the release record so operators
can compare the source digest, mirrored digest, pair digest, and runtime digest
pin during change review.

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

oras copy --to-oci-layout \
  <source-registry>/<source-namespace>/codeprobe-release-pair:<version> \
  codeprobe-release-pair:<version>

tar -cf codeprobe-agent-referrers-<version>.tar codeprobe-agent-referrers
tar -cf codeprobe-scoring-referrers-<version>.tar codeprobe-scoring-referrers
tar -cf codeprobe-release-pair-<version>.tar codeprobe-release-pair

sha256sum \
  codeprobe-agent-<version>.tar \
  codeprobe-scoring-<version>.tar \
  codeprobe-agent-referrers-<version>.tar \
  codeprobe-scoring-referrers-<version>.tar \
  codeprobe-release-pair-<version>.tar

skopeo copy --all --preserve-digests \
  oci-archive:codeprobe-agent-<version>.tar \
  docker://<private-registry>/<private-namespace>/codeprobe-agent:<version>

skopeo copy --all --preserve-digests \
  oci-archive:codeprobe-scoring-<version>.tar \
  docker://<private-registry>/<private-namespace>/codeprobe-scoring:<version>

tar -xf codeprobe-agent-referrers-<version>.tar
tar -xf codeprobe-scoring-referrers-<version>.tar
tar -xf codeprobe-release-pair-<version>.tar

oras copy --recursive --from-oci-layout \
  codeprobe-agent-referrers:<version> \
  <private-registry>/<private-namespace>/codeprobe-agent@sha256:<agent-digest>

oras copy --recursive --from-oci-layout \
  codeprobe-scoring-referrers:<version> \
  <private-registry>/<private-namespace>/codeprobe-scoring@sha256:<scoring-digest>

oras copy --from-oci-layout \
  codeprobe-release-pair:<version> \
  <private-registry>/<private-namespace>/codeprobe-release-pair:<version>
```

While still online, download GitHub attestation bundles and trusted roots for
both image digests:

```bash
mkdir -p github-attestation-agent github-attestation-scoring

(cd github-attestation-agent && \
  gh attestation download \
    'oci://<source-registry>/<source-namespace>/codeprobe-agent@sha256:<agent-digest>' \
    --repo '<source-owner>/<source-repo>')

(cd github-attestation-scoring && \
  gh attestation download \
    'oci://<source-registry>/<source-namespace>/codeprobe-scoring@sha256:<scoring-digest>' \
    --repo '<source-owner>/<source-repo>')

gh attestation trusted-root > github-trusted-root.jsonl
```

Also prepare Cosign v3 local verification material while online:

```bash
cosign initialize
cp ~/.sigstore/root/tuf-repo-cdn.sigstore.dev/targets/trusted_root.json \
  cosign-trusted-root.json

cosign save \
  '<source-registry>/<source-namespace>/codeprobe-agent@sha256:<agent-digest>' \
  --dir cosign-agent-local

cosign save \
  '<source-registry>/<source-namespace>/codeprobe-scoring@sha256:<scoring-digest>' \
  --dir cosign-scoring-local
```

Transfer the image archives, referrer archives, release-pair archive,
`github-attestation-*` directories, `github-trusted-root.jsonl`,
`cosign-*-local` directories, and `cosign-trusted-root.json` through the
approved offline channel. After import, verify the destination registry digests
before trusting any local tag:

```bash
AGENT_DEST_DIGEST=$(skopeo inspect --format '{{.Digest}}' \
  docker://<private-registry>/<private-namespace>/codeprobe-agent:<version>)
SCORING_DEST_DIGEST=$(skopeo inspect --format '{{.Digest}}' \
  docker://<private-registry>/<private-namespace>/codeprobe-scoring:<version>)

test "$AGENT_DEST_DIGEST" = 'sha256:<agent-digest>'
test "$SCORING_DEST_DIGEST" = 'sha256:<scoring-digest>'
```

Verify GitHub artifact attestations offline against the destination digests with
the saved bundle and trusted root:

```bash
gh attestation verify \
  'oci://<private-registry>/<private-namespace>/codeprobe-agent@sha256:<agent-digest>' \
  --bundle github-attestation-agent/<bundle-file>.jsonl \
  --custom-trusted-root github-trusted-root.jsonl \
  --repo '<source-owner>/<source-repo>' \
  --source-ref 'refs/tags/v<version>' \
  --source-digest '<release-sha>' \
  --cert-identity '<release-workflow-identity>' \
  --cert-oidc-issuer 'https://token.actions.githubusercontent.com'

gh attestation verify \
  'oci://<private-registry>/<private-namespace>/codeprobe-scoring@sha256:<scoring-digest>' \
  --bundle github-attestation-scoring/<bundle-file>.jsonl \
  --custom-trusted-root github-trusted-root.jsonl \
  --repo '<source-owner>/<source-repo>' \
  --source-ref 'refs/tags/v<version>' \
  --source-digest '<release-sha>' \
  --cert-identity '<release-workflow-identity>' \
  --cert-oidc-issuer 'https://token.actions.githubusercontent.com'
```

Verify Cosign signatures offline with the saved local image/signature material
and trusted root:

```bash
cosign verify --local-image cosign-agent-local \
  --trusted-root cosign-trusted-root.json \
  --certificate-identity '<release-workflow-identity>' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com'

cosign verify --local-image cosign-scoring-local \
  --trusted-root cosign-trusted-root.json \
  --certificate-identity '<release-workflow-identity>' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com'
```

Pull and verify the transferred release-pair authority offline before
configuring runtime refs from any imported image digest:

```bash
oras pull \
  <private-registry>/<private-namespace>/codeprobe-release-pair:<version>

cosign verify-blob \
  --bundle release-pair.bundle \
  --trusted-root cosign-trusted-root.json \
  --certificate-identity '<release-workflow-identity>' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  release-pair.json

test "$(jq -r '.source.sha' release-pair.json)" = '<release-sha>'
test "$(jq -r '.source.ref' release-pair.json)" = 'refs/tags/v<version>'

PAIR_AGENT_DIGEST=$(jq -r \
  '.images[] | select(.image == "codeprobe-agent") | .digest' \
  release-pair.json)
PAIR_SCORING_DIGEST=$(jq -r \
  '.images[] | select(.image == "codeprobe-scoring") | .digest' \
  release-pair.json)

test "$PAIR_AGENT_DIGEST" = "$AGENT_DEST_DIGEST"
test "$PAIR_SCORING_DIGEST" = "$SCORING_DEST_DIGEST"
```

Record the archive checksums, resulting private-registry digests, verification
inputs, and verification output in the change ticket or release evidence store.
Do not replace digest pins with mutable operator-local tags after transfer.
