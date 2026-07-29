# Provider Support Coordination Guide

Provider support owns asynchronous scheduling, the external session, and the
intervention record. The engagement stays lightweight and never grants
provider personnel access to the data-owner environment.

## Asynchronous intake

Send [`intake-and-consent.md`](templates/intake-and-consent.md) as an
asynchronous form with a ten-minute maximum data-owner budget. Ask the
data owner to omit repository names, URLs, paths, code, credentials, logs, and
diagnostics. Resolve scheduling asynchronously; do not add a discovery
interview.

The data owner confirms only readiness facts: technical-owner authority,
supported runtime, local containment, agent availability, network posture,
sample-plan readiness, the selected comparison dimension, and consent to the
boundary.

## Structured session

Cap the staff-engineer session at 45 minutes:

| Minutes | Activity |
| --- | --- |
| 0–5 | Reconfirm roles, prohibited data, and stop conditions |
| 5–15 | Data owner reports install, validation, and dry-run status |
| 15–30 | Data owner operates the approved run or resumes it locally |
| 30–40 | Data owner reports coded completion, attrition, and gate status |
| 40–45 | Confirm next local step, approval ownership, and asynchronous follow-up |

The data owner owns the keyboard. Do not use remote shell, screen control,
repository access, or screen sharing that exposes prohibited data. If a command
fails, use its public error code and a data-owner-sanitized description. Never
request the raw terminal, files, logs, diagnostics, or result rows.

Record every interaction in
[`intervention-log.json`](templates/intervention-log.json). A session may
continue only while all provider events remain structurally permitted.

## Optional follow-up

A security or platform follow-up is optional and asynchronous by default. It
may clarify public requirements or sanitized error codes. It must not inspect
the environment, weaken containment, bypass validation, receive prohibited
data, or become a prerequisite for a valid run.

## Coordination outcomes

- **Ready:** data owner continues locally under the frozen plan.
- **Waiting on data owner:** preserve the plan and resume asynchronously.
- **Platform blocked:** optional sanitized follow-up; no gate waiver.
- **Insufficient evidence:** record the bounded outcome and stop.
- **Disqualified:** stop the run; internal remediation may precede a new
  data-owner-owned run.

Dogfood and internal-rehearsal scheduling use the same documents. Internal
operators must still run independently so the shared-repository exercise
measures repeatability rather than paired operation.
