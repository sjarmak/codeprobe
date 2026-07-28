# Zero-Code-Access Operator Kit

This kit is the executable operating contract for `CP-ZCA-PILOT-2026`. A
participant technical owner runs CodeProbe inside an environment controlled by
the participant. Solutions Engineering (SE), Field Engineering (FE), Core
Engineering, and all other Sourcegraph personnel remain outside that
environment and never receive repository, source, prompt, patch, trace,
task-level result, raw result, log, or diagnostic access.

The machine-readable invariants are in
[`kit-contract.json`](kit-contract.json). The governing strategy is
[`zero_code_access_validation.md`](../../strategy/zero_code_access_validation.md),
and the export boundary is
[`EVIDENCE_BUNDLE.md`](../../EVIDENCE_BUNDLE.md).

## Who uses what

| Owner | Required material |
| --- | --- |
| Participant technical owner | [Participant runbook](participant-runbook.md), [intake and consent](templates/intake-and-consent.md), [sampling plan](templates/sampling-plan.md), [experiment profile](templates/experiment.template.json), and [evidence request](templates/evidence-request.template.json) |
| Solutions Engineering | [SE methodology](se-methodology.md) and [bounded findings review](templates/bounded-findings.md) |
| Field Engineering | [FE coordination guide](fe-coordination.md), intake, scheduling, and the intervention log |
| Participant security/platform | Optional follow-up only; no mandatory meeting or repository access |
| Core Engineering | Internal defect repair only; any involvement in an external run disqualifies that run |

## Required sequence

1. FE sends the asynchronous intake. The participant spends no more than ten
   minutes on it and omits repository-identifying or prohibited data.
2. The participant and SE freeze the sampling plan and two-configuration
   profile before results exist.
3. The participant installs, configures, mines, validates, and dry-runs locally.
4. FE coordinates one structured session capped at 45 minutes. The participant
   operates every command and shares only coded status or sanitized,
   non-identifying observations.
5. The participant runs both configurations on the same task set with at least
   ten paired distinct scorable tasks and three repeats per task and
   configuration.
6. The participant aggregates and inspects all results locally, completes the
   local evidence request, previews the exact five artifacts, and either
   rejects or explicitly approves that exact preview.
7. Only the approved five-file directory may leave the environment. SE reviews
   that bundle against the bounded findings template.

## Stop conditions

Stop the external proof and record a disqualifying intervention if any
Sourcegraph person accesses the environment or source, provides bespoke code,
repairs evidence, receives prohibited data, or reinterprets raw results. A fix
may be developed and verified internally, but the participant must begin a new
external run.

Return `insufficient_evidence` without weakening the gate when fewer than ten
paired tasks remain, repeats are incomplete, task sets differ, the sample
changed after results, representativeness is not attested, the comparison is
refused, or support was disqualifying.
