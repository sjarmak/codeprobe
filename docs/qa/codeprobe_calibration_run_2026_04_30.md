# codeprobe calibration triad — corpus run (2026-04-30 19:46 UTC)

This report tabulates the null / golden / adversarial fixture scores against every task in the corpus. Each fixture is a synthetic agent output scored through the production scoring path. Band breaches mean the task's rubric does not enforce the expected reward range for that fixture.

## Reward bands

| Fixture | Band (inclusive) | Intent |
|---|---|---|
| null | ≤ 0.1 | empty agent output should not credit any task |
| golden | ≥ 0.9 | the expected solution should max out the rubric |
| adversarial | ≤ 0.5 | echoing oracle tokens with distractor noise should leave precision low enough that the headline reward is at most partial credit |

## Per-fixture pass rates

| Fixture | Pass | Fail | Pass rate | Mean reward |
|---|---|---|---|---|
| null | 7 | 21 | 25.0% | 0.750 |
| golden | 28 | 0 | 100.0% | 1.000 |
| adversarial | 7 | 21 | 25.0% | 0.806 |

## Per-task table

| Task | reward_type | scorer_family | null | golden | adversarial | all_pass |
|---|---|---|---|---|---|---|
| 0f2b0737 | continuous | continuous | ✓ 0.000 | ✓ 1.000 | ✓ 0.310 | ✓ |
| 17d154d1 | continuous | continuous | ✓ 0.000 | ✓ 1.000 | ✓ 0.322 | ✓ |
| 1f9bbd7d | continuous | continuous | ✓ 0.000 | ✓ 1.000 | ✓ 0.322 | ✓ |
| 3878c832 | continuous | continuous | ✓ 0.000 | ✓ 1.000 | ✓ 0.298 | ✓ |
| 81279ad7 | continuous | continuous | ✓ 0.000 | ✓ 1.000 | ✓ 0.310 | ✓ |
| add-docstring | binary | binary_test | ✗ 1.000 | ✓ 1.000 | ✗ 1.000 | ✗ |
| add-logging | binary | binary_test | ✗ 1.000 | ✓ 1.000 | ✗ 1.000 | ✗ |
| add-null-check | binary | binary_test | ✗ 1.000 | ✓ 1.000 | ✗ 1.000 | ✗ |
| add-type-hint | binary | binary_test | ✗ 1.000 | ✓ 1.000 | ✗ 1.000 | ✗ |
| count-classes | binary | binary_test | ✗ 1.000 | ✓ 1.000 | ✗ 1.000 | ✗ |
| count-functions | binary | binary_test | ✗ 1.000 | ✓ 1.000 | ✗ 1.000 | ✗ |
| count-test-files | binary | binary_test | ✗ 1.000 | ✓ 1.000 | ✗ 1.000 | ✗ |
| detect-test-framework | binary | binary_test | ✗ 1.000 | ✓ 1.000 | ✗ 1.000 | ✗ |
| dual_task | binary | binary_test | ✗ 1.000 | ✓ 1.000 | ✗ 1.000 | ✗ |
| extract-helper | binary | binary_test | ✗ 1.000 | ✓ 1.000 | ✗ 1.000 | ✗ |
| find-config-format | binary | binary_test | ✗ 1.000 | ✓ 1.000 | ✗ 1.000 | ✗ |
| find-entrypoint | binary | binary_test | ✗ 1.000 | ✓ 1.000 | ✗ 1.000 | ✗ |
| fix-import | binary | binary_test | ✗ 1.000 | ✓ 1.000 | ✗ 1.000 | ✗ |
| fix-off-by-one | binary | binary_test | ✗ 1.000 | ✓ 1.000 | ✗ 1.000 | ✗ |
| handle-edge-case | binary | binary_test | ✗ 1.000 | ✓ 1.000 | ✗ 1.000 | ✗ |
| identify-modified-files | binary | binary_test | ✗ 1.000 | ✓ 1.000 | ✗ 1.000 | ✗ |
| list-direct-dependencies | binary | binary_test | ✗ 1.000 | ✓ 1.000 | ✗ 1.000 | ✗ |
| list-public-api | binary | binary_test | ✗ 1.000 | ✓ 1.000 | ✗ 1.000 | ✗ |
| probe-findfunction-001 | exact_match | binary_test | ✓ 0.000 | ✓ 1.000 | ✓ 0.000 | ✓ |
| probe-returntype-000 | exact_match | binary_test | ✓ 0.000 | ✓ 1.000 | ✓ 0.000 | ✓ |
| raise-specific-exception | binary | binary_test | ✗ 1.000 | ✓ 1.000 | ✗ 1.000 | ✗ |
| rename-variable | binary | binary_test | ✗ 1.000 | ✓ 1.000 | ✗ 1.000 | ✗ |
| trace-dependency | binary | binary_test | ✗ 1.000 | ✓ 1.000 | ✗ 1.000 | ✗ |

## Breach clusters

### null (reward outside [0.00, 0.10])

21 task(s):

- `add-docstring`
- `add-logging`
- `add-null-check`
- `add-type-hint`
- `count-classes`
- `count-functions`
- `count-test-files`
- `detect-test-framework`
- `dual_task`
- `extract-helper`
- `find-config-format`
- `find-entrypoint`
- `fix-import`
- `fix-off-by-one`
- `handle-edge-case`
- `identify-modified-files`
- `list-direct-dependencies`
- `list-public-api`
- `raise-specific-exception`
- `rename-variable`
- `trace-dependency`

### adversarial (reward outside [0.00, 0.50])

21 task(s):

- `add-docstring`
- `add-logging`
- `add-null-check`
- `add-type-hint`
- `count-classes`
- `count-functions`
- `count-test-files`
- `detect-test-framework`
- `dual_task`
- `extract-helper`
- `find-config-format`
- `find-entrypoint`
- `fix-import`
- `fix-off-by-one`
- `handle-edge-case`
- `identify-modified-files`
- `list-direct-dependencies`
- `list-public-api`
- `raise-specific-exception`
- `rename-variable`
- `trace-dependency`

## Rubric clusters (fixture × scorer_family)

- **adversarial × binary_test** → 21 breach(es): `add-docstring`, `add-logging`, `add-null-check`, `add-type-hint`, `count-classes`, `count-functions`, `count-test-files`, `detect-test-framework` …
- **null × binary_test** → 21 breach(es): `add-docstring`, `add-logging`, `add-null-check`, `add-type-hint`, `count-classes`, `count-functions`, `count-test-files`, `detect-test-framework` …

