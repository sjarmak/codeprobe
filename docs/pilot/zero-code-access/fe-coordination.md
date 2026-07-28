# Field Engineering Coordination Guide

FE owns asynchronous scheduling, the external session, and the intervention
record. The engagement stays lightweight and never grants Sourcegraph access to
the participant environment.

## Asynchronous intake

Send [`intake-and-consent.md`](templates/intake-and-consent.md) as an
asynchronous form with a ten-minute maximum participant budget. Ask the
participant to omit repository names, URLs, paths, code, credentials, logs, and
diagnostics. Resolve scheduling asynchronously; do not add a discovery
interview.

The participant confirms only readiness facts: technical-owner authority,
supported runtime, local containment, agent availability, network posture,
sample-plan readiness, the selected comparison dimension, and consent to the
boundary.

## Structured session

Cap the staff-engineer session at 45 minutes:

| Minutes | Activity |
| --- | --- |
| 0–5 | Reconfirm roles, prohibited data, and stop conditions |
| 5–15 | Participant reports install, validation, and dry-run status |
| 15–30 | Participant operates the approved run or resumes it locally |
| 30–40 | Participant reports coded completion, attrition, and gate status |
| 40–45 | Confirm next local step, approval ownership, and asynchronous follow-up |

The participant owns the keyboard. Do not use remote shell, screen control,
repository access, or screen sharing that exposes prohibited data. If a command
fails, use its public error code and a participant-sanitized description. Never
request the raw terminal, files, logs, diagnostics, or result rows.

Record every interaction in
[`intervention-log.json`](templates/intervention-log.json). A session may
continue only while all Sourcegraph events remain structurally permitted.

## Optional follow-up

A security or platform follow-up is optional and asynchronous by default. It
may clarify public requirements or sanitized error codes. It must not inspect
the environment, weaken containment, bypass validation, receive prohibited
data, or become a prerequisite for a valid run.

## Coordination outcomes

- **Ready:** participant continues locally under the frozen plan.
- **Waiting on participant:** preserve the plan and resume asynchronously.
- **Platform blocked:** optional sanitized follow-up; no gate waiver.
- **Insufficient evidence:** record the bounded outcome and stop.
- **Disqualified:** stop the run; internal remediation may precede a new
  participant-owned run.

Dogfood and internal-rehearsal scheduling use the same documents. Internal
operators must still run independently so the shared-repository exercise
measures repeatability rather than paired operation.
