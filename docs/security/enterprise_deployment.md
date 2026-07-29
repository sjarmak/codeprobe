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
`src/codeprobe/sandbox/image_bootstrap.py`,
`src/codeprobe/sandbox/image_config.py`, `src/codeprobe/sandbox/runner.py`,
`src/codeprobe/cli/bootstrap_cmd.py`, `src/codeprobe/sandbox/Dockerfile.agent`,
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
| `codeprobe bootstrap` | Configured OCI registry in online mode; none in paired archive mode | Online mode asks the container engine to pull each digest-pinned image. Archive mode requires no network egress and validates local OCI archives with Skopeo. Registry authentication, proxy, and private-CA trust belong to the engine. |
| `codeprobe mine` | Git remotes, optional PR or issue APIs, optional LLM backend, optional MCP or code-search endpoint | Narrative and enrichment quality depend on configured sources. `--no-llm` and offline fallbacks reduce egress but may lower task quality. |
| `codeprobe run` agent execution | Model API or configured agent gateway; configured MCP/tool endpoints when the agent arm enables them | Agent containers run with `--network=bridge` because the agent must reach the model API. |
| Scoring and verifier execution | None in container mode | The scoring container uses `--network=none`. Host-consented verifier execution inherits the host network. |
| `codeprobe check-infra offline` | None | Credential TTL checks read environment variables only. |
| `codeprobe snapshot create`, `snapshot verify`, `snapshot evidence`, `snapshot export` | None | These commands are local transforms. Uploading generated artifacts is outside CodeProbe. |
| `codeprobe purge` | None | Deletes only scoped local artifacts. |

CodeProbe does not restrict agent-container bridge egress by destination. Use
host, container-engine, or network controls when an enterprise policy requires
an endpoint allowlist.

## Container Images and Mounts

The published image names are versioned CodeProbe labels. Their base images
are pinned by SHA-256 digest:
`node:22-bookworm-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3`
for `codeprobe-agent:0.14.0rc1` and
`debian:bookworm-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818`
for `codeprobe-scoring:0.14.0rc1`. Debian package installation uses a dated
snapshot. The Claude Code version and npm integrity are pinned, and both
images declare the non-root `codeprobe` user. The release workflow emits SBOM
and provenance attestations, scans both images, and signs their immutable
digests. Runtime consumers must use the verified digest identities rather
than treating a version tag as an integrity boundary.

Prepare the released agent and scoring images from an installed wheel. Replace
the example digests with the verified release or private-registry digests:

```bash
codeprobe bootstrap \
  --agent-image 'registry.example.test/platform/codeprobe-agent@sha256:<agent-digest>' \
  --scoring-image 'registry.example.test/platform/codeprobe-scoring@sha256:<scoring-digest>'
```

The released source image names for this package version are
`codeprobe-agent:0.14.0rc1` and `codeprobe-scoring:0.14.0rc1`. Bootstrap works with
Docker or Podman and needs no repository checkout or Dockerfiles. It verifies
both expected digests before atomically recording immutable local IDs. Private
registry authentication, proxy configuration, and private CA trust remain
operator-controlled engine settings. For air-gapped loading, pass both
`--agent-archive` and `--scoring-archive`; Skopeo validates and imports the OCI
archives.

Mount matrix:

| Execution path | Network | Host mount | Container mount | Mode |
| --- | --- | --- | --- | --- |
| Agent container | `--network=bridge` | per-task slot worktree | identical path | `rw` |
| Agent container | `--network=bridge` | per-slot `CLAUDE_CONFIG_DIR`, when present | identical path | `rw` |
| Agent container | `--network=bridge` | generated MCP config temp file | identical path | `ro` |
| Agent container | `--network=bridge` | validated private CA files and directories | collision-safe path under `/etc/codeprobe/ca` | `ro` |
| Scoring container | `--network=none` | scoring temp directory | identical path | `rw` |

The primary checkout and the user's global agent config directory are not
mounted into the agent container. A host-global `CLAUDE_CONFIG_DIR` is not used
as a fallback mount. The container receives a per-slot config dir only when
session isolation produced one.

Claude session isolation ensures credential files are private regular files
inside that per-slot directory. A hardlink is used when possible so in-place
OAuth refreshes remain coherent; the copy fallback keeps mode `0600`. Other
read-mostly host configuration entries remain symlinks to the live host
configuration, including settings, skills, agents, hooks, plugins, commands,
and rules. Mounting the per-slot directory therefore exposes only that selected
session layout to the agent container even though the host-global config
directory is not mounted directly. Mutable session state is recreated inside
the slot.

The container posture is containment, not a hardened multi-tenant sandbox.
Both images declare a non-root `codeprobe` user, and runtime commands also map
to the invoking host UID and GID where supported. Agent and scoring containers
use `--cap-drop=ALL`, `--security-opt=no-new-privileges`, `--read-only`, a
bounded writable `/tmp`, `--cpus=2`, `--memory=4g`, and `--pids-limit=256`.
They retain the container engine's default seccomp profile rather than
shipping a custom seccomp profile. Operators running untrusted tasks should
still use a dedicated workstation, VM, or CI runner and keep the host kernel
and container engine patched.

## Credentials and Environment

Agent subprocesses receive a filtered environment from
`src/codeprobe/adapters/_base.py`, not the full parent process environment.
The host subprocess adapter environment whitelist is:

| Variable | Host purpose | Agent container behavior |
| --- | --- | --- |
| `ALL_PROXY` | Proxy URL | Passthrough |
| `ANTHROPIC_API_KEY` | Agent or LLM backend API key | Passthrough |
| `ANTHROPIC_AUTH_TOKEN` | Enterprise LLM gateway auth token | Passthrough |
| `ANTHROPIC_BASE_URL` | Enterprise LLM gateway URL | Passthrough |
| `CARGO_HOME` | Rust toolchain path | Excluded because host paths are not mounted |
| `CLAUDE_CODE_OAUTH_TOKEN` | Agent OAuth token | Passthrough |
| `CLAUDE_CONFIG_DIR` | Per-slot agent config directory | Passthrough only when that directory is mounted |
| `CODEPROBE_SANDBOX` | User-set containment signal | Passthrough |
| `COPILOT_GITHUB_TOKEN` | Copilot GitHub token | Passthrough |
| `COPILOT_MODEL` | Copilot model override | Passthrough |
| `COPILOT_OFFLINE` | Copilot offline-provider toggle | Passthrough |
| `COPILOT_PROVIDER_API_KEY` | Copilot offline-provider credential | Passthrough |
| `COPILOT_PROVIDER_BASE_URL` | Copilot offline-provider endpoint | Passthrough |
| `COPILOT_PROVIDER_TYPE` | Copilot offline-provider type | Passthrough |
| `CURL_CA_BUNDLE` | Private CA bundle path | Excluded from valueless passthrough; mounted `ro` and rewritten |
| `DBUS_SESSION_BUS_ADDRESS` | Desktop session bus path | Excluded because host paths are not mounted |
| `GH_TOKEN` | GitHub CLI token usable by Copilot | Passthrough |
| `GITHUB_TOKEN` | Git provider token for agent tooling | Passthrough |
| `GOPATH` | Go toolchain path | Excluded because host paths are not mounted |
| `GOROOT` | Go toolchain path | Excluded because host paths are not mounted |
| `HOME` | Host home path | Excluded because host paths are not mounted |
| `HTTPS_PROXY` | Proxy URL | Passthrough |
| `HTTP_PROXY` | Proxy URL | Passthrough |
| `LANG` | Locale | Passthrough |
| `LC_ALL` | Locale | Passthrough |
| `LOGNAME` | User identity metadata | Passthrough |
| `NODE_EXTRA_CA_CERTS` | Private CA bundle path | Excluded from valueless passthrough; mounted `ro` and rewritten |
| `NODE_PATH` | Node toolchain path | Excluded because host paths are not mounted |
| `NO_PROXY` | Proxy bypass list | Passthrough |
| `NPM_CONFIG_PREFIX` | npm toolchain path | Excluded because host paths are not mounted |
| `OPENAI_API_KEY` | Agent or LLM backend API key | Passthrough |
| `PATH` | Host executable search path | Excluded because the image owns its toolchain |
| `PYTHONPATH` | Python import path | Excluded because host paths are not mounted |
| `REQUESTS_CA_BUNDLE` | Private CA bundle path | Excluded from valueless passthrough; mounted `ro` and rewritten |
| `RUSTUP_HOME` | Rust toolchain path | Excluded because host paths are not mounted |
| `SSL_CERT_DIR` | Private CA directory path | Excluded from valueless passthrough; mounted `ro` and rewritten |
| `SSL_CERT_FILE` | Private CA file path | Excluded from valueless passthrough; mounted `ro` and rewritten |
| `TERM` | Terminal metadata | Passthrough |
| `TMPDIR` | Host temp path | Excluded because host paths are not mounted |
| `USER` | User identity metadata | Passthrough |
| `VIRTUAL_ENV` | Python virtualenv path | Excluded because host paths are not mounted |
| `XDG_CONFIG_HOME` | Desktop config path | Excluded because host paths are not mounted |
| `XDG_DATA_HOME` | Desktop data path | Excluded because host paths are not mounted |
| `XDG_RUNTIME_DIR` | Desktop runtime path | Excluded because host paths are not mounted |
| `all_proxy` | Proxy URL | Passthrough |
| `http_proxy` | Proxy URL | Passthrough |
| `https_proxy` | Proxy URL | Passthrough |
| `no_proxy` | Proxy bypass list | Passthrough |

Additional process-level variables are:

| Variable | Purpose | Container behavior |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | Agent or LLM backend API key | Forwarded by key name |
| `CLAUDE_CODE_OAUTH_TOKEN` | Agent OAuth token | Forwarded by key name |
| `CLAUDE_CONFIG_DIR` | Per-slot agent config directory | Forwarded only when that directory is mounted |
| `OPENAI_API_KEY` | Agent or LLM backend API key | Forwarded by key name |
| `COPILOT_GITHUB_TOKEN` | Copilot GitHub token | Forwarded by key name |
| `GH_TOKEN` | GitHub CLI token usable by Copilot | Forwarded by key name |
| `GITHUB_TOKEN` | Git provider token for agent tooling | Forwarded by key name |
| `COPILOT_OFFLINE` | Copilot offline-provider toggle | Forwarded by key name |
| `COPILOT_PROVIDER_BASE_URL` | Copilot offline-provider endpoint | Forwarded by key name |
| `COPILOT_PROVIDER_API_KEY` | Copilot offline-provider credential | Forwarded by key name |
| `COPILOT_PROVIDER_TYPE` | Copilot offline-provider type | Forwarded by key name |
| `COPILOT_MODEL` | Copilot model override | Forwarded by key name |
| `ANTHROPIC_BASE_URL` | Enterprise LLM gateway URL | Forwarded by key name |
| `ANTHROPIC_AUTH_TOKEN` | Enterprise LLM gateway auth token | Forwarded by key name |
| `CODEPROBE_SANDBOX` | User-set containment signal | Read by sandbox detection |
| `CODEPROBE_CONTAINER_CONFIG` | Absolute prepared-image record path | Read by bootstrap and container runtime |
| `CODEPROBE_OFFLINE` | Offline guard signal | Set by `codeprobe run --offline` after preflight |
| `CODEPROBE_SIGNING_KEY` | Snapshot HMAC key | Read by snapshot signing |
| `AWS_SESSION_EXPIRATION` | Bedrock session expiration | Read by offline preflight |
| `AWS_CREDENTIAL_EXPIRATION` | Bedrock fallback credential expiration | Read by offline preflight |
| `GOOGLE_APPLICATION_CREDENTIALS_TOKEN_EXPIRY` | Vertex token expiration | Read by offline preflight |
| `AZURE_TOKEN_EXPIRES_ON` | Azure OpenAI token expiration | Read by offline preflight |

Secrets passed into an agent container are emitted as valueless `-e` arguments
using only the variable name, so secret values do not appear in the container
argv. MCP configuration values may expand environment variables into a
temporary JSON file. That file can contain secrets in cleartext until cleanup
succeeds; `codeprobe purge` also sweeps stale `codeprobe-mcp-*.json` files.

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
from valueless container passthrough: `SSL_CERT_FILE`, `SSL_CERT_DIR`,
`REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, and `NODE_EXTRA_CA_CERTS`.

During agent-container preparation, CodeProbe validates private CA files and
directories, resolves and deduplicates their host paths, mounts them read-only
under `/etc/codeprobe/ca`, and rewrites the container environment values to the
collision-safe container paths. Invalid or unreadable paths are not mounted;
`codeprobe doctor` reports the selected agent's invalid CA configuration before
the run. These runtime mounts do not configure the container engine itself:
registry pulls and bootstrap still use the operator-controlled engine trust
store.

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

Application logs are stdout and stderr from the CLI process. JSON output,
terminal transcripts, shell history, CI logs, and container engine logs are
operator-managed and can contain paths, diagnostics, command lines, and
fragments of verifier output. CodeProbe does not provide a central log store or
log-retention policy.

## Local Artifacts, Retention, and Deletion

Run artifacts are local, retained until the operator deletes them, and may
contain proprietary source in cleartext. CodeProbe does not encrypt local run
artifacts at rest. The v1 control is operator-managed encrypted storage for
the workspace, CI runner, and artifact volume, plus `codeprobe purge`
retention enforcement.

Deletion responsibilities:

| Artifact | Delete with | Notes |
| --- | --- | --- |
| `.codeprobe/` | Operator filesystem deletion or `codeprobe purge . --all --yes` | Whole experiment directory removal. |
| `.codeprobe/<experiment>/runs/` | `codeprobe purge . --yes` | Removes transcripts, checkpoints, and trace storage for each experiment. |
| `.codeprobe/<experiment>/runs/trace.db` | `codeprobe purge . --yes` | Contains redacted trace rows, but source snippets are possible when a tool output is not covered by a redaction rule. |
| `.codeprobe/<experiment>/runs/<config>/<task>/` | `codeprobe purge . --yes` | Contains task transcripts such as `agent_output.txt` and `agent_error.txt`. |
| `codeprobe-mcp-*.json` | `codeprobe purge . --yes` | Swept from the system temp directory when it is a regular file and not a symlink. |
| Task worktrees | Operator temp/worktree cleanup | Created outside `.codeprobe/`; purge does not delete them. |
| Per-slot agent session directories | Operator temp/session cleanup | Includes per-slot `CLAUDE_CONFIG_DIR` contents when session isolation created them, including private credential hardlinks or copies outside `.codeprobe/` result artifacts. |
| Scoring temp directories | Operator temp cleanup | Purge does not delete scoring temp directories outside `.codeprobe/`. |
| `SNAPSHOT_DIR` | Operator filesystem deletion | Snapshot output is outside `.codeprobe/` unless the operator places it there. |
| `evidence` | Operator filesystem deletion | Evidence bundle output is outside `.codeprobe/` unless the operator places it there. |
| Export directories | Operator filesystem deletion | Includes observability and spreadsheet exports. |
| CI logs, terminal transcripts, shell history, and backups | Operator platform controls | Purge cannot delete records retained by CI, backup, shell, or terminal systems. |

`codeprobe purge` does not delete snapshots, evidence bundles, export
directories, task worktrees, per-slot agent session directories, scoring temp
directories, CI logs, terminal transcripts, shell history, or backups.

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
codeprobe snapshot evidence export request.json --out evidence --approve sha256:DIGEST --no-json
codeprobe snapshot evidence validate evidence --expect sha256:DIGEST --no-json
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
