# Predeclared Sampling Plan

Keep this worksheet inside the participant environment. Freeze it before
viewing results. Export only the schema-approved hashes and counts produced
through the evidence request.

## Scope

- Strategy: `CP-ZCA-PILOT-2026`
- Selection method: `predeclared_explicit` / `predeclared_stratified` /
  `predeclared_window`
- Window start (`YYYY-MM-DD`):
- Window end (`YYYY-MM-DD`):
- Mining goal: `quality` / `navigation` / `mcp` / `general`
- Candidate count, maximum 20:
- Minimum paired distinct scorable tasks: `10`
- Repeats per task and configuration: `3`

## Comparison

- Configuration A local description:
- Configuration B local description:
- The one dimension that differs:
- All controlled fields held equal:
- [ ] Both arms use the identical frozen task set.
- [ ] Neither arm receives information produced by the other arm.

Local descriptions may contain sensitive detail and must not enter the evidence
request. Record only configuration SHA-256 digests there.

## Anonymous task mix

Assign stable anonymous categories before results:

| Category | Selection rule | Selected count | Paired scorable count |
| --- | --- | ---: | ---: |
| `category_01` | Local-only description |  |  |
| `category_02` | Local-only description |  |  |

Allowed exclusion codes are `predeclared`, `duplicate`, `out_of_window`, and
`unsupported_task_type`. Record counts, not task identities, in the evidence
request.

## Hashes-only attestation

For every selected task, record locally:

| Anonymous row | Task SHA-256 | Verifier SHA-256 | Category |
| --- | --- | --- | --- |
| 01 |  |  | `category_01` |

- [ ] Task digests are unique.
- [ ] Category selected counts equal the number of hashed task rows.
- [ ] Attrition equals selected minus paired scorable counts.
- [ ] At least ten paired rows remain.
- [ ] The sample did not change after results were visible.
- [ ] The participant technical owner attests the frozen sample represents the
  declared workload.

If any final checkbox is false, use `insufficient_evidence`.
