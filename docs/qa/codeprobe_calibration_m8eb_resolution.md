# codeprobe-m8eb resolution: synthetic flag for placeholder example tasks

## Surfaced by

The 2026-04-30 19:46 UTC corpus run of `codeprobe calibrate-triad`
(see `docs/qa/codeprobe_calibration_run_2026_04_30.md` history) flagged
21 band breaches. The dated report itself is regenerated on every triad
run, so this companion document is the durable record of the fix.

## Symptom

21 of 28 corpus tasks failed the null fixture (reward should be ≤ 0.1
for empty agent output) AND the adversarial fixture (reward should be
≤ 0.5 for a haystack of distractors). All 21 scored `reward=1.000` for
both null and adversarial agent output. Every offender was either
under `examples/dual/{comprehension,sdlc}/*` or
`tests/fixtures/dual_task`. Their `tests/test.sh` was uniformly:

```
#!/usr/bin/env bash
set -e
exit 0
```

i.e. the test ignored the agent output entirely.

## Decision: Option B — `synthetic = true` flag

`examples/dual/README.md` already documented these tasks as
illustrative of the dual-verification file shape, not runnable
benchmarks. We adopted Option B from the bead (the lower-risk path):

1. Every example `task.toml` now carries `synthetic = true` under
   `[metadata]`.
2. `discover_calibration_tasks` filters tasks where the metadata flag
   is set, by default.
3. A new `--include-synthetic` flag on `codeprobe calibrate-triad`
   restores the old behaviour for callers who want to inspect the
   synthetic corpus.

Option A (rewriting `tests/test.sh` for each example to do real
verification) was rejected because the README already promises these
tasks are skeletons; converting them would change the contract the
file states downstream copiers can rely on.

## Acceptance check

- [x] Decision recorded (this document)
- [x] `codeprobe calibrate-triad --strict` exits 0 against the
  post-fix default corpus — verified 2026-04-30 21:59 UTC; 7 tasks
  pass null/golden/adversarial bands.
- [x] `codeprobe calibrate-triad --include-synthetic --strict`
  reproduces the original 21 breaches (intended diagnostic path).

## Files changed

- `src/codeprobe/calibration/triad.py` — `is_synthetic_task()` helper
  and `include_synthetic` parameter on `discover_calibration_tasks`.
- `src/codeprobe/cli/calibrate_triad_cmd.py` — `--include-synthetic`
  CLI flag, threaded through `_resolve_task_dirs`.
- 21 × `task.toml` under `examples/dual/{comprehension,sdlc}/*` and
  `tests/fixtures/dual_task/` — added `synthetic = true`.
- `tests/calibration/test_triad.py` — discovery + CLI coverage for the
  new flag and `is_synthetic_task` helper.
- `tests/test_examples_dual.py` — regression guard asserting every
  example carries `synthetic = true` so future copies cannot silently
  re-leak placeholders into the calibration corpus.
- `examples/dual/README.md` — documents the flag and warns copiers to
  remove it once they implement real verification.
