# Security Policy

## Supported Versions

CodeProbe is beta, pre-1.0 software. Routine security fixes target the latest
released minor and the current development branch. The immediately preceding
published minor receives upgrade assistance for 90 days; it does not receive
routine fixes. Maintainers may explicitly backport a critical fix.

The current package version is declared in `pyproject.toml`. Upgrade to the
latest release before reporting a vulnerability that may already be fixed.
The complete platform, schema, deprecation, and migration boundary is versioned
in `docs/support.md` and `docs/support_policy.json`.

## Reporting a Vulnerability

Report suspected vulnerabilities through the repository's private advisory
channel:
`https://github.com/sjarmak/codeprobe/security/advisories/new`.

If that channel is unavailable in a fork or mirror, open a public issue that
asks maintainers to establish a private channel. Do not include exploit details,
proprietary source, credentials, agent transcripts, trace output, or
reproduction data that contains secrets.

Include:

- affected CodeProbe version and install source;
- the command or workflow involved;
- whether the run used `--uncontained`, containerized execution, or
  `CODEPROBE_SANDBOX=1`;
- whether `.codeprobe/` artifacts, snapshots, or evidence bundles were
  produced; and
- a minimal reproduction that does not include private repository content.

Maintainers will triage the report, confirm the affected boundary, and decide
whether the fix is a code change, a documentation correction, or an operational
mitigation.

## Enterprise Data Handling

CodeProbe executes autonomous coding agents and mined verifier scripts. Local
run artifacts can contain proprietary source in cleartext. The enterprise
threat model and data-handling contract are maintained in
`docs/security/enterprise_deployment.md`.
