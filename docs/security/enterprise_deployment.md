# Enterprise Deployment Security

This guide is the enterprise threat model and data-handling contract for
CodeProbe. It describes the current implementation, not a target state.

The machine-checked inventory for this guide lives in
`docs/security/enterprise_inventory.json` and is enforced by
`tests/lint/test_enterprise_security_docs.py`. Source anchors for the claims in
this document include `src/codeprobe/adapters/_base.py`,
`src/codeprobe/cli/check_infra.py`, `src/codeprobe/cli/purge_cmd.py`,
`src/codeprobe/cli/run_cmd.py`, `src/codeprobe/core/containment.py`,
`src/codeprobe/core/scoring/sandbox.py`, `src/codeprobe/net/credential_ttl.py`,
`src/codeprobe/net/offline.py`, `src/codeprobe/sandbox/agent_container.py`,
`src/codeprobe/sandbox/runner.py`, `src/codeprobe/sandbox/Dockerfile.agent`,
`src/codeprobe/sandbox/Dockerfile.scoring`, `src/codeprobe/snapshot/redact.py`,
`src/codeprobe/trace/content_policy.py`, and `src/codeprobe/trace/store.py`.
Related public contracts are `SECURITY.md`, `README.md`,
`docs/SNAPSHOT_REDACTION.md`, `docs/EVIDENCE_BUNDLE.md`, and
`docs/adapters.md`.

## System Summary

CodeProbe turns repository history into evaluation tasks, runs one or more
coding agents against isolated worktrees, scores the results, and records
outcomes. It can also create local snapshots and zero-code-access evidence
bundles for review.

CodeProbe executes:

- git commands against the target repository;
- agent subprocesses or backend SDK calls;
- mined `test.sh` and verifier scripts;
- Docker or Podman containers when the required images are available; and
- local snapshot, export, validation, and purge commands.

CodeProbe reads:

- repository source and history;
- `.codeprobe/` experiment metadata, tasks, and prior run artifacts;
- security-relevant environment variables documented below;
- per-agent configuration such as `CLAUDE_CONFIG_DIR`; and
- generated MCP configuration temp files named `codeprobe-mcp-*.json`.

CodeProbe writes:

- `.codeprobe/<experiment>/runs/` results, transcripts, checkpoints, and
  `trace.db`;
- isolated task worktrees and scoring temp directories;
- per-slot agent session directories in temp storage for agents that need
  session isolation;
- snapshots and zero-code-access evidence bundles when requested; and
- local export artifacts for downstream systems.

## Threat Model

Protected assets:

- proprietary repository source and diffs;
- prompts, instructions, task metadata, transcripts, traces, and verifier
  outputs;
- API keys, OAuth tokens, gateway tokens, proxy credentials, and short-lived
  cloud credentials;
- experiment configuration, results, aggregate reports, and evidence bundles;
  and
- the operator workstation or CI runner that invokes CodeProbe.

Trust boundaries:

| Boundary | Current contract |
| --- | --- |
| Operator host or CI runner | Trusted to hold source, credentials, container engine state, and local artifacts. Local OS compromise is out of scope for CodeProbe controls. |
| Agent worktree | Isolated per task, created from committed state. A dirty primary checkout is refused unless `--allow-dirty` is passed. |
| Agent process | Autonomous code execution. Container mode limits mounts but still gives model egress. Host-consented mode gives the agent host filesystem, credential, and network access. |
| Scoring process | Mined third-party verifier code. Container scoring uses `--network=none`; host fallback requires an already contained environment or explicit `--uncontained` consent. |
| Model, git, and tool providers | External trust boundary. Content sent to those endpoints is governed by the operator's provider contracts. |
| Snapshot and evidence export | Local file boundary. Export commands write files only; the operator decides whether and where to transmit them. |

Attacker capabilities considered:

- a malicious or compromised mined verifier script;
- a malicious or compromised agent process;
- prompt or tool output that includes secrets or proprietary source;
- stale or unexpected MCP configuration temp files;
- accidental publication of local run artifacts;
- network endpoints that log prompts, tool requests, or source snippets; and
- symlink or path manipulation near deletion and export boundaries.

Out of scope:

- a compromised host kernel or container runtime;
- malicious maintainers of the target repository;
- provider-side retention or training policy outside CodeProbe's control; and
- operator mistakes after local export files have been created.

## Egress Matrix

| Subsystem | Required egress | Notes |
| --- | --- | --- |
| `codeprobe mine` | Git remotes, optional PR or issue APIs, optional LLM backend, optional MCP or code-search endpoint | Narrative and enrichment quality depend on configured sources. `--no-llm` and offline fallbacks reduce egress but may lower task quality. |
| `codeprobe run` agent execution | Model API or configured agent gateway; configured MCP/tool endpoints when the agent arm enables them | Agent containers run with `--network=bridge` because the agent must reach the model API. |
| Scoring and verifier execution | None in container mode | The scoring container uses `--network=none`. Host-consented verifier execution inherits the host network. |
| `codeprobe check-infra offline` | None | Credential TTL checks read environment variables only. |
| `codeprobe snapshot create`, `snapshot verify`, `snapshot evidence`, `snapshot export` | None | These commands are local transforms. Uploading generated artifacts is outside CodeProbe. |
| `codeprobe purge` | None | Deletes only scoped local artifacts. |

## Container Images and Mounts

Build the agent image:

```bash
docker build -f src/codeprobe/sandbox/Dockerfile.agent -t codeprobe-agent:0.12 .
```

Build the scoring image:

```bash
docker build -f src/codeprobe/sandbox/Dockerfile.scoring -t codeprobe-scoring:0.12 .
```

Mount matrix:

| Execution path | Network | Host mount | Container mount | Mode |
| --- | --- | --- | --- | --- |
| Agent container | `--network=bridge` | per-task slot worktree | identical path | `rw` |
| Agent container | `--network=bridge` | per-slot `CLAUDE_CONFIG_DIR`, when present | identical path | `rw` |
| Agent container | `--network=bridge` | generated MCP config temp file | identical path | `ro` |
| Scoring container | `--network=none` | scoring temp directory | identical path | `rw` |

The primary checkout and the user's global agent config directory are not
mounted into the agent container. A host-global `CLAUDE_CONFIG_DIR` is not used
as a fallback mount. The container receives a per-slot config dir only when
session isolation produced one.

The container posture is containment, not a hardened multi-tenant sandbox. The
Dockerfiles do not add a non-root user or custom seccomp profile. Operators
running untrusted tasks should run CodeProbe on a dedicated workstation, VM, or
CI runner and keep the container engine patched.

## Credentials and Environment

Agent subprocesses receive a filtered environment from
`src/codeprobe/adapters/_base.py`, not the full parent process environment.
The documented credential and routing variables are:

| Variable | Purpose | Container behavior |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | Agent or LLM backend API key | Forwarded by key name |
| `CLAUDE_CODE_OAUTH_TOKEN` | Agent OAuth token | Forwarded by key name |
| `CLAUDE_CONFIG_DIR` | Per-slot agent config directory | Forwarded only when that directory is mounted |
| `OPENAI_API_KEY` | Agent or LLM backend API key | Forwarded by key name |
| `COPILOT_API_KEY` | Agent API key | Forwarded by key name |
| `GITHUB_TOKEN` | Git provider token for agent tooling | Forwarded by key name |
| `ANTHROPIC_BASE_URL` | Enterprise LLM gateway URL | Forwarded by key name |
| `ANTHROPIC_AUTH_TOKEN` | Enterprise LLM gateway auth token | Forwarded by key name |
| `CODEPROBE_SANDBOX` | User-set containment signal | Read by sandbox detection |
| `CODEPROBE_OFFLINE` | Offline guard signal | Set by `codeprobe run --offline` after preflight |
| `CODEPROBE_SIGNING_KEY` | Snapshot HMAC key | Read by snapshot signing |

Secrets passed into an agent container are emitted as valueless `-e KEY`
arguments, so secret values do not appear in the container argv. MCP
configuration values may expand environment variables into a temporary JSON
file. That file can contain secrets in cleartext until cleanup succeeds;
`codeprobe purge` also sweeps stale `codeprobe-mcp-*.json` files.

Short-lived credential TTL checks are available for:

| Variable | Backend preflight purpose |
| --- | --- |
| `AWS_SESSION_EXPIRATION` | Bedrock session expiration |
| `AWS_CREDENTIAL_EXPIRATION` | Bedrock fallback credential expiration |
| `GOOGLE_APPLICATION_CREDENTIALS_TOKEN_EXPIRY` | Vertex token expiration |
| `AZURE_TOKEN_EXPIRES_ON` | Azure OpenAI token expiration |

Run the offline preflight directly:

```bash
codeprobe check-infra offline --json
```

Or run with the preflight wired into `codeprobe run`:

```bash
codeprobe run . --offline --offline-expected-run-duration 1h
```

## Proxy and Private CA Behavior

Host subprocess execution passes these proxy variables when present:
`HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, `ALL_PROXY`, `http_proxy`,
`https_proxy`, `no_proxy`, and `all_proxy`.

The same proxy URL variables are forwarded into the agent container. Private CA
host-path variables are whitelisted for host subprocess execution but excluded
from container passthrough because the referenced host files are not mounted:
`SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, and
`NODE_EXTRA_CA_CERTS`.

For containerized enterprise runs that require a private CA, build a custom
agent image from `src/codeprobe/sandbox/Dockerfile.agent` with the CA installed
inside the image, or add a reviewed wrapper that mounts the CA path explicitly.
Do not assume a host CA path will work inside the container.

## Offline Guarantees

`codeprobe run --offline` performs a credential TTL preflight and then sets
`CODEPROBE_OFFLINE=1`. Network-touching Python subsystems that call
`guard_offline` fail loud with `OFFLINE_NET_ATTEMPT`.

There is no socket-level interception. The offline guard is opt-in at known
network call sites; it does not monkeypatch sockets or block egress from
external binaries. Agent processes own their network behavior. Scoring
containers still use `--network=none` when the scoring image is available.

## Local Telemetry and Redaction

`trace.db` stores agent lifecycle events under
`.codeprobe/<experiment>/runs/trace.db`. Tool inputs and outputs are redacted
before insertion by `src/codeprobe/trace/content_policy.py`:

- exact live environment values of length at least eight become
  `[REDACTED-ENV]`;
- authorization and token-shaped strings become `[REDACTED-AUTH]`; and
- user-supplied `--trace-deny` globs can replace matching tool outputs with
  `[REDACTED-GLOB]`.

Agent transcripts under `.codeprobe/<experiment>/runs/<config>/<task>/` receive
secret-token and auth-pattern redaction only. Source code printed by the agent
is not redacted.

## Local Artifacts, Retention, and Deletion

Run artifacts are local, retained until the operator deletes them, and may
contain proprietary source in cleartext. CodeProbe does not encrypt local run artifacts at rest.
The v1 control is operator-managed encrypted storage for the workspace, CI
runner, and artifact volume, plus `codeprobe purge` retention enforcement.

Use the purge command as the retention lever:

```bash
codeprobe purge .
codeprobe purge . --yes
codeprobe purge . --older-than 30 --yes
codeprobe purge . --all --yes
```

`codeprobe purge` lists candidates by default. With `--yes`, it deletes
`.codeprobe/` run artifacts, or whole experiment directories when `--all` is
passed, plus stale `codeprobe-mcp-*.json` temp files. It never touches your
source tree and never runs git.

Operators own backup policy. If `.codeprobe/`, worktree temp directories,
snapshot outputs, or evidence bundles are included in endpoint backup or CI
artifact backup, those systems become part of the retention boundary.

## Export Boundaries

The default shareable snapshot is hashes-only:

```bash
codeprobe snapshot create EXPERIMENT_DIR --out SNAPSHOT_DIR --redact hashes-only
codeprobe snapshot verify SNAPSHOT_DIR
```

`hashes-only` snapshots include paths, sizes, hashes, and attestation metadata,
but no file bodies. Content-bearing modes require
`--allow-source-in-export`; see `docs/SNAPSHOT_REDACTION.md`.

Zero-code-access evidence bundles are stricter than snapshots. They export a
fixed five-file bundle that excludes file names, file sizes, prompts, patches,
traces, task-level results, raw diagnostics, and free-form identifying text:

```bash
codeprobe snapshot evidence preview request.json --no-json
codeprobe snapshot evidence export request.json --out approved-evidence --approve sha256:DIGEST --no-json
codeprobe snapshot evidence validate approved-evidence --expect sha256:DIGEST --no-json
```

Observability and spreadsheet exporters are local transforms. The operator is
responsible for reviewing generated files before uploading them to any external
system.

## Incident Response Responsibilities

Operator responsibilities:

- run CodeProbe in a contained or dedicated environment;
- manage encrypted storage for local artifacts;
- set retention windows and run `codeprobe purge`;
- rotate any credential exposed through prompts, transcripts, traces, temp
  MCP config files, snapshots, logs, or exported artifacts;
- preserve relevant `.codeprobe/` artifacts only when needed for forensics; and
- review generated export files before transmission.

CodeProbe maintainer responsibilities:

- fix defects in containment, redaction, deletion, or export code;
- correct documentation that overstates implemented controls;
- add drift tests when a documented security claim becomes machine-checkable;
  and
- publish security fixes for supported versions under `SECURITY.md`.

If a secret may have been written to local artifacts, treat the artifact volume,
CI logs, terminal transcripts, snapshots, and backups as possible exposure
locations. Rotate first, then purge or quarantine affected files according to
the operator's incident process.
