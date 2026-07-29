# Security Policy

## Supported Versions

CodeProbe is pre-1.0 software. Security fixes are made against the latest
released minor version and the current development branch. Older releases are
not supported unless maintainers explicitly choose a backport for a critical
issue.

The current package version is declared in `pyproject.toml`. Upgrade to the
latest release before reporting a vulnerability that may already be fixed.

## Reporting a Vulnerability

Report suspected vulnerabilities through the repository's private
security-reporting channel when one is available. If no private channel is
available, open a public issue that asks maintainers to establish a private
channel, but do not include exploit details, proprietary source, credentials,
agent transcripts, trace output, or reproduction data that contains secrets.

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
