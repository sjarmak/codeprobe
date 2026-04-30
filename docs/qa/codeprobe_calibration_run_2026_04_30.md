# codeprobe calibration triad — corpus run (2026-04-30 22:00 UTC)

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
| null | 7 | 0 | 100.0% | 0.000 |
| golden | 7 | 0 | 100.0% | 1.000 |
| adversarial | 7 | 0 | 100.0% | 0.223 |

## Per-task table

| Task | reward_type | scorer_family | null | golden | adversarial | all_pass |
|---|---|---|---|---|---|---|
| 0f2b0737 | continuous | continuous | ✓ 0.000 | ✓ 1.000 | ✓ 0.310 | ✓ |
| 17d154d1 | continuous | continuous | ✓ 0.000 | ✓ 1.000 | ✓ 0.322 | ✓ |
| 1f9bbd7d | continuous | continuous | ✓ 0.000 | ✓ 1.000 | ✓ 0.322 | ✓ |
| 3878c832 | continuous | continuous | ✓ 0.000 | ✓ 1.000 | ✓ 0.298 | ✓ |
| 81279ad7 | continuous | continuous | ✓ 0.000 | ✓ 1.000 | ✓ 0.310 | ✓ |
| probe-findfunction-001 | exact_match | binary_test | ✓ 0.000 | ✓ 1.000 | ✓ 0.000 | ✓ |
| probe-returntype-000 | exact_match | binary_test | ✓ 0.000 | ✓ 1.000 | ✓ 0.000 | ✓ |

