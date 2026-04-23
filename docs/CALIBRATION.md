# Calibration (R11)

Calibration measures how much to trust curator-derived scores by checking
that two independent curators agree on a hand-annotated holdout set.

codeprobe ships the **code path and schema** for this gate. The partner-gated
DATA — the holdout itself — is deliberately out of scope for the codebase
and must be produced by domain partners. This file describes both halves so
the handoff is unambiguous.

## Gate (enforced in code)

A `CalibrationProfile` is emitted only when all three conditions hold:

1. Holdout contains **>= 100** task rows.
2. Holdout spans **>= 3** distinct repositories.
3. Pearson correlation between the two curators is **>= 0.6** (default;
   override with `--threshold`).

If any condition fails, `codeprobe calibrate` prints the rejection reason
to stderr and exits non-zero. No profile is written.

The gate is implemented in [`src/codeprobe/calibration/gate.py`](../src/codeprobe/calibration/gate.py)
and the schema in [`src/codeprobe/calibration/profile.py`](../src/codeprobe/calibration/profile.py).

## Holdout dataset format

`codeprobe calibrate HOLDOUT_PATH` expects `HOLDOUT_PATH` to be a JSON file
containing an array of objects with these fields:

| Field       | Type    | Meaning                                            |
| ----------- | ------- | -------------------------------------------------- |
| `task_id`   | string  | Stable identifier for the task                     |
| `curator_a` | number  | First curator's score (convention: [0, 1])         |
| `curator_b` | number  | Second curator's score (convention: [0, 1])        |
| `repo`      | string  | Repository identifier for this task                |

Example:

```json
[
  {"task_id": "repo_a__pr_123", "curator_a": 0.8, "curator_b": 0.75, "repo": "repo_a"},
  {"task_id": "repo_b__pr_045", "curator_a": 0.4, "curator_b": 0.35, "repo": "repo_b"}
]
```

## Partner-data acquisition (out of code scope)

Producing a valid holdout requires coordination with partners and is **not**
something codeprobe automates. The process is owned by the Phase 0 discovery
partners and lives outside this repository.

### Requirements for a valid partner holdout

- **>= 3 non-OSS repositories.** OSS-only holdouts are not acceptable for
  R11 because the curator signal leaks through public PR review quality
  norms. Partner repos keep the evaluation closer to real-world enterprise
  codebases.
- **>= 100 tasks total.** Correlation estimates below this threshold are too
  noisy to justify the downstream "trust the curator" inference.
- **Two independent curators** scoring each task without visibility into
  each other's judgments. Independence is a partner process requirement,
  not something code can verify.
- **Provenance record** kept by the partner so the holdout can be audited
  later (dates, curators, repos, task selection methodology). codeprobe
  does not store this — it only consumes the resulting JSON.

### Handoff procedure

1. Partners run their two curators on the agreed holdout task list and
   record per-task scores on both sides.
2. Partners export to the JSON shape documented above.
3. Partners run `codeprobe calibrate HOLDOUT_PATH --curator-version <id>`.
4. If the gate passes, the emitted profile JSON is shared with the codeprobe
   deployment (e.g. pointed to by `CODEPROBE_CALIBRATION_PROFILE`).
5. If the gate fails, partners iterate on the holdout (larger n, more
   repos, better curator calibration) until it passes. **No profile is
   emitted until the gate passes.**

### Why the gate is strict

R11 exists to prevent "calibration theatre" — reporting a
`calibration_confidence` number that is based on too little data to be
meaningful. The code refuses to emit in that regime on purpose.

## Testing with synthetic data (this repository)

The test suite under `tests/calibration/` feeds synthetic holdout rows
(pure-Python lists) to the gate. This exercises the code path end-to-end
without partner data. See:

- [`tests/calibration/test_gate.py`](../tests/calibration/test_gate.py) — 
  gate acceptance and rejection.
- [`tests/calibration/test_stability.py`](../tests/calibration/test_stability.py) —
  deterministic scoring across repeated runs.
- [`tests/cli/test_assess_calibration_surface.py`](../tests/cli/test_assess_calibration_surface.py) —
  `codeprobe assess` surfaces `calibration_confidence`.

Synthetic tests are explicitly not a substitute for a partner-produced
holdout. They verify the code contract only.

## Surfacing calibration in `codeprobe assess`

Set the `CODEPROBE_CALIBRATION_PROFILE` environment variable to a path
containing a valid profile JSON. `codeprobe assess` will load it and print
a `calibration_confidence` line. When the variable is unset or the file is
missing/malformed, assess prints `calibration_confidence: unavailable` — the
field is always present so downstream tooling can depend on its shape.
