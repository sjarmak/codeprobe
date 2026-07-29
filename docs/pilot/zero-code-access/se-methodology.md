# Solutions Engineering Methodology

SE owns the method, not the data-owner environment. For an external proof, SE
never operates commands, requests screen access, receives prohibited data, or
interprets raw results. The data-owner technical owner controls the sample,
execution, local analysis, evidence request, and approval.

## Before execution

1. Confirm the participant has the
   [runbook](participant-runbook.md), the
   [sampling plan](templates/sampling-plan.md), and the reviewed CodeProbe
   version.
2. Confirm the sample was frozen before results: time window, selection method,
   anonymous task mix, exclusions, and the rule for attrition.
3. Confirm the profile has exactly A and B, changes one declared dimension,
   uses the same task set, and requires three repeats.
4. Confirm the participant expects at least ten paired distinct scorable tasks
   after attrition.
5. Confirm FE will log every intervention using the fixed actor-role and event
   codes.

Do not approve a sample's semantic representativeness. That judgment belongs to
the data-owner technical owner and is recorded as an attestation.

## During execution

Permitted help is limited to the published runbook, generic guidance,
asynchronous coordination, the bounded live session, sanitized diagnostics,
and an optional security follow-up. Work only from participant-reported coded
status. Do not ask for repository identifiers, paths, source, prompts, patches,
traces, task-level results, raw results, logs, or diagnostics.

Stop the external proof if Provider Engineering participates or if any provider
person directly accesses the environment, provides bespoke code, repairs
evidence, receives prohibited data, or reinterprets raw results.

## Evidence review

Accept only a directory containing the exact five artifacts listed in
[`EVIDENCE_BUNDLE.md`](../../EVIDENCE_BUNDLE.md). Do not accept archives of
`.codeprobe/`, screenshots, local aggregate reports, ad hoc redactions, or
supplemental files.

Use [`bounded-findings.md`](templates/bounded-findings.md) to check:

- the approval digest is consistent;
- the sample contains at least ten paired distinct tasks and three repeats;
- A and B used the same task set;
- the participant attested privacy, sample fidelity, result fidelity, and
  usefulness;
- support was not disqualifying; and
- the conclusion is exactly `advance_a`, `advance_b`, or
  `insufficient_evidence`.

The fixed exported `findings.md` is the report of record. The review worksheet
must not add claims about ROI, productivity causality, production outcomes,
procurement readiness, or customer-grade calibration.

## Internal rehearsal

For the internal stage, one SE and one FE independently run this same protocol
on a shared authorized repository, then exercise it on a second authorized
repository. Internal access does not relax the task floor, repeats, sampling,
logging, or evidence gate. Internal results prepare operators but cannot
replace the external proof.
