# Bounded Findings Review

Use this worksheet to review the participant-approved bundle. It is not an
additional export artifact and must not contain supplemental customer data.
The generated `findings.md` remains the report of record.

## Bundle integrity

- [ ] The directory contains exactly `run-manifest.json`,
  `sample-attestation.json`, `aggregate-results.json`, `findings.md`, and
  `support-log.json`.
- [ ] One approval digest binds every artifact and the technical-owner
  attestation.
- [ ] Configuration identities are anonymous A and B with SHA-256 digests.
- [ ] Task provenance contains only digests, anonymous categories, and counts.
- [ ] The support log contains only coded roles and event kinds.

## Gate

- [ ] At least ten paired distinct scorable tasks remain.
- [ ] Each task and configuration has three scorable repeats.
- [ ] A and B used the same task set.
- [ ] The sample was frozen before results and attested representative.
- [ ] The comparison was not structurally refused.
- [ ] No disqualifying support occurred.

If any gate item is false, the only valid conclusion is
`insufficient_evidence`.

## Bounded conclusion

Select exactly the value already present in the approved bundle:

- [ ] `advance_a`
- [ ] `advance_b`
- [ ] `insufficient_evidence`

The conclusion applies only to the two anonymized configurations, frozen
sample, recorded CodeProbe version, participant environment, and executed
runs. It does not establish organization-wide ROI, causal productivity,
production-outcome prediction, procurement readiness, security certification,
or customer-grade calibration.

## Participant sign-off

- [ ] Privacy approved.
- [ ] Sample fidelity approved.
- [ ] Result fidelity approved.
- [ ] Usefulness approved, including refusal to advance.
