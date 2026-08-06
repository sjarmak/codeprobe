# Enterprise support and compatibility

Policy version: **2026.1**. Effective with CodeProbe **0.13.0**.
`docs/support_policy.json` is the machine-readable source for release checks.

CodeProbe is beta software: the documented paths below are supported, but the
public contract is not yet a 1.0 stability promise. “Supported” means the path
is exercised by blocking CI or the release gate and maintainers accept defect
reports. “Preview” means the code path exists but lacks the same end-to-end
coverage. “Unsupported” means maintainers may ask for reproduction on a
supported path before investigating.

## Support matrix

| Dimension | Supported and tested | Preview | Unsupported |
| --- | --- | --- | --- |
| Python | CPython 3.11, 3.12, and 3.13 | None | 3.10 and older; 3.14 and newer |
| Host OS | Ubuntu 22.04 LTS Linux | Other glibc-based x86_64 Linux; macOS for mining only | Windows, musl-only Linux |
| Architecture | `linux/amd64` host and OCI; release OCI smoke on `linux/arm64` | arm64 host CLI | 32-bit and non-Linux OCI |
| Container engine | Docker on the pinned Ubuntu 22.04 release runner | Podman 4.x/5.x on Linux | Engines without digest and read-only mount support |
| Git | Git 2.34+ on Linux, full clone | None | Older Git; shallow clones for mining |
| Agent | Claude Code 2.1.220 in the published digest-pinned agent image | Copilot CLI 1.0.4+ on the host | Quarantined Codex adapter for repository-edit comparisons; unregistered CLIs |
| Repository | Python, Go, JavaScript/TypeScript quality mining | Python-only architecture comprehension | Other primary languages |

The wheel is pure Python, but that does not imply support on every platform
where pip can unpack it. `requires-python` intentionally matches the tested
3.11–3.13 range. Published OCI manifests cover amd64 and arm64; the
release-blocking real-agent journey currently runs on amd64.

macOS is preview, and only for mining. What earns it that status is one CI job
(`mining-macos`) running `tests/mining` on a macOS runner, which covers the
platform-specific piece — publication renames a staged directory into place
with `renameatx_np`/`RENAME_EXCL` there instead of Linux's
`renameat2`/`RENAME_NOREPLACE`. Nothing else is exercised on macOS: agent and
scoring containers, the acceptance loop, and the release gates all run on the
pinned Ubuntu runner. Treat any other macOS path as untested.

## Compatibility windows

Patch releases preserve documented flags, exit codes, envelope v1 fields, and
persisted artifact reads within the current minor. Before 1.0, a minor release
may add fields or retire a deprecated surface only under the notice policy
below.

| Surface | Candidate writes | Candidate reads |
| --- | --- | --- |
| CLI | envelope v1 and command payload v1 | v1 envelopes from 0.11–0.13; consumers must ignore added fields |
| Configuration | current `experiment.json` | unversioned 0.11–0.13 experiment files; legacy booleans are normalized |
| Task | current `task.toml` plus `metadata.json` | task directories written by 0.11–0.13 |
| Result | current per-config `results.json` | results written by 0.11–0.13; missing newer fields receive documented legacy defaults |
| Snapshot | hardened schema 1.0 | hardened 1.0 only; unsafe legacy hashes-only directories containing bodies are refused |
| Evidence | the exact five `codeprobe.zero-code-access.*.v1` artifacts | that exact v1 set only; unknown, missing, or extra fields fail closed |

Unsigned/unversioned configuration, task, and result formats are bounded by
producer release, not guessed from shape. The release upgrade gate uses the
published 0.11.0 wheel to generate all three and proves the candidate can read
them. A future change outside this window must add an explicit schema version
and migration or a dedicated refusal; silent coercion is not allowed.

## Deprecation and removal

A normal removal requires all of the following:

1. notice in `CHANGELOG.md`, this policy, and a structured CLI warning;
2. at least one full minor release and 90 calendar days of notice;
3. a replacement command or artifact migration documented before removal; and
4. an upgrade fixture proving the old surface migrates or refuses with an
   actionable error.

Patch releases do not remove documented surfaces. An unsafe behavior may be
disabled immediately: the release notes must name the security boundary and
the CLI must return a dedicated error with safe recovery instructions. The
legacy hashes-only snapshot refusal follows this exception because those
directories can contain source bodies despite their label.

## Upgrade procedure

1. Preserve the original `.codeprobe/` directory and snapshots; do not edit
   signed or digest-bound artifacts in place.
2. Install the candidate wheel into a fresh environment or upgrade the existing
   virtual environment.
3. Run `codeprobe validate PATH/.codeprobe/tasks --json`.
4. Run `codeprobe interpret PATH --format json --json` to verify saved results.
5. Run `codeprobe snapshot verify SNAPSHOT --json` for each retained snapshot.
   On `SNAPSHOT_UNSAFE_LEGACY_FORMAT`, recreate from the original experiment
   with `codeprobe snapshot create ... --redact hashes-only`; never repair the
   old directory.
6. Run `codeprobe skills migrate --dry-run`, review the plan, then rerun without
   `--dry-run` if legacy user-home skills are reported.

The blocking release harness installs the exact published 0.11.0 wheel
(`a7797…bfbf5`), generates a task, experiment, measured result, and snapshot,
upgrades that environment to the exact candidate wheel, and exercises these
steps. It accepts safe reads and requires the dedicated unsafe-snapshot refusal.

## Source-free support bundle

Do not send `.codeprobe/`, prompts, patches, traces, task directories, repository
paths, environment dumps, or raw `doctor` output. A support-safe bundle contains
only:

- CodeProbe version and wheel SHA-256;
- policy version `2026.1`;
- OS family, architecture, Python minor, Git version, and container-engine
  name/version;
- selected agent name and CLI version, never its credential or config;
- structured error code, command name, exit code, and redacted check statuses;
- candidate OCI digest references; and
- when relevant, the already-reviewed five-file zero-code-access evidence
  bundle and its approval digest.

Use coded values rather than free-form diagnostics. Review the bundle locally
before transfer. The fixed evidence bundle is the only supported results
attachment; a hashes-only snapshot still exposes paths and sizes and is not a
substitute.

## Support ownership

Repository maintainers own supported-path defects, compatibility decisions,
and migration guidance. Operators own credentials, containment configuration,
storage, retention, and local review before disclosure. Agent vendors own their
CLI and model-service availability. Container-engine vendors own engine defects.

Report product defects through the repository issue tracker using only the
source-free fields above. Report vulnerabilities through the private advisory
channel in `SECURITY.md`. Only the latest released minor receives routine fixes;
the immediately preceding published minor receives upgrade assistance for 90
days. Critical backports remain an explicit maintainer decision, not an implied
service-level agreement.
