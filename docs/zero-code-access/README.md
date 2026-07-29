# Zero-Code-Access Operator Kit

This kit is the executable operating contract for
`codeprobe.zero-code-access.operator.v1`. A data-owner technical owner runs
CodeProbe inside an environment controlled by the data owner. Provider
Engineering and all other provider personnel remain outside that environment
and never receive repository, source, prompt, patch, trace, task-level result,
raw result, log, or diagnostic access.

The machine-readable invariants are in
[`kit-contract.json`](kit-contract.json), and the export boundary is
[`EVIDENCE_BUNDLE.md`](../EVIDENCE_BUNDLE.md).

## Who uses what

| Owner | Required material |
| --- | --- |
| Data-owner technical owner | [Data-owner runbook](data-owner-runbook.md), [intake and consent](templates/intake-and-consent.md), [sampling plan](templates/sampling-plan.md), [experiment profile](templates/experiment.template.json), and [evidence request](templates/evidence-request.template.json) |
| Provider support | [Support methodology](support-methodology.md), [coordination guide](coordination.md), intake, scheduling, the intervention log, and [bounded findings review](templates/bounded-findings.md) |
| Data-owner security/platform | Optional follow-up only; no mandatory meeting or repository access |
| Provider Engineering | Internal defect repair only; any involvement in an external run disqualifies that run |

## Required sequence

1. Provider support sends the asynchronous intake. The data owner spends no more than ten
   minutes on it and omits repository-identifying or prohibited data.
2. The data owner and provider support freeze the sampling plan and two-configuration
   profile before results exist.
3. The data owner installs, configures, mines, validates, and dry-runs locally.
4. Provider support coordinates one structured session capped at 45 minutes. The data owner
   operates every command and shares only coded status or sanitized,
   non-identifying observations.
5. The data owner runs both configurations on the same task set with at least
   ten paired distinct scorable tasks and three repeats per task and
   configuration.
6. The data owner aggregates and inspects all results locally, completes the
   local evidence request, previews the exact five artifacts, and either
   rejects or explicitly approves that exact preview.
7. Only the approved five-file directory may leave the environment. Provider support reviews
   that bundle against the bounded findings template.

## Stop conditions

Stop the external proof and record a disqualifying intervention if any
provider person accesses the environment or source, provides bespoke code,
repairs evidence, receives prohibited data, or reinterprets raw results. A fix
may be developed and verified internally, but the data owner must begin a new
external run.

Return `insufficient_evidence` without weakening the gate when fewer than ten
paired tasks remain, repeats are incomplete, task sets differ, the sample
changed after results, representativeness is not attested, the comparison is
refused, or support was disqualifying.
