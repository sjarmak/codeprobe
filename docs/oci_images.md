# OCI Image Contract

CodeProbe publishes two execution images for each release:

- `codeprobe-agent` runs the agent subprocess with network access for model API calls.
- `codeprobe-scoring` runs mined test and verifier scripts with `--network=none`.

Release tags build both images for `linux/amd64` and `linux/arm64`. The workflow
pushes only the immutable package version tag, records the pushed digest in the
`oci-image-identity-*` workflow artifacts, produces SBOM/provenance attestations,
scans the digest reference, and signs the digest with keyless Cosign.

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

## Private-Registry Mirroring

Mirror the release tag and all platform manifests with a registry-aware copier:

```bash
skopeo copy --all \
  docker://<source-registry>/<source-namespace>/codeprobe-agent:<version> \
  docker://<private-registry>/<private-namespace>/codeprobe-agent:<version>

skopeo copy --all \
  docker://<source-registry>/<source-namespace>/codeprobe-scoring:<version> \
  docker://<private-registry>/<private-namespace>/codeprobe-scoring:<version>
```

After mirroring, inspect the private-registry digests and configure exact
runtime references:

```bash
skopeo inspect --raw \
  docker://<private-registry>/<private-namespace>/codeprobe-agent:<version>

export CODEPROBE_AGENT_IMAGE='<private-registry>/<private-namespace>/codeprobe-agent@sha256:<agent-digest>'
export CODEPROBE_SCORING_IMAGE='<private-registry>/<private-namespace>/codeprobe-scoring@sha256:<scoring-digest>'
```

Keep the workflow's `oci-image-identity-*` JSON artifacts with the release record
so operators can compare the source digest, mirrored digest, and runtime digest
pin during change review.

## Offline OCI Archive Transfer

For an airgapped registry, copy the multi-platform image into an OCI archive,
move it through the approved offline channel, then import it into the destination
registry:

```bash
skopeo copy --all \
  docker://<source-registry>/<source-namespace>/codeprobe-agent:<version> \
  oci-archive:codeprobe-agent-<version>.tar

skopeo copy --all \
  docker://<source-registry>/<source-namespace>/codeprobe-scoring:<version> \
  oci-archive:codeprobe-scoring-<version>.tar

sha256sum codeprobe-agent-<version>.tar codeprobe-scoring-<version>.tar

skopeo copy --all \
  oci-archive:codeprobe-agent-<version>.tar \
  docker://<private-registry>/<private-namespace>/codeprobe-agent:<version>

skopeo copy --all \
  oci-archive:codeprobe-scoring-<version>.tar \
  docker://<private-registry>/<private-namespace>/codeprobe-scoring:<version>
```

Record the archive checksums and resulting private-registry digests in the
change ticket or release evidence store. Do not replace digest pins with mutable
operator-local tags after transfer.
