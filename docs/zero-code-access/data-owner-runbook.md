# Data-Owner Runbook

The data-owner technical owner performs every step in the data-owner
environment. Do not screen-share source, repository paths, prompts, patches,
traces, task-level results, raw results, logs, or diagnostics. Provider support may
receive only coded progress and sanitized, non-identifying observations.

Keep the full `.codeprobe/` directory, the sampling worksheet, and the local
evidence request inside the environment. Only the approved five-artifact
directory is shareable.

## 1. Complete and freeze local inputs

Complete these before viewing results:

- [`intake-and-consent.md`](templates/intake-and-consent.md)
- [`sampling-plan.md`](templates/sampling-plan.md)
- [`experiment.template.json`](templates/experiment.template.json)

Review the profile fields and predeclare the one comparison dimension in the
sampling plan. Do not add repository names, URLs, credentials, or secret values
to any material intended for export.

## 2. Install, configure, and preflight

Use an approved CPython 3.11, 3.12, or 3.13 environment and install the reviewed CodeProbe
release through the data owner's normal package process. Set
`CODEPROBE_VENV` to a data-owner-approved path outside the target repository:

```bash
python3 -m venv "$CODEPROBE_VENV"
. "$CODEPROBE_VENV/bin/activate"
python -m pip install "codeprobe==$CODEPROBE_VERSION"
codeprobe --version
codeprobe doctor
```

Provider support provides the reviewed release number out of band as
`CODEPROBE_VERSION`.
Refuse an unpinned install or a runtime version that differs from the reviewed
release.

Set `CODEPROBE_KIT` to the downloaded kit directory, then materialize the
profile:

```bash
codeprobe experiment init . --non-interactive --no-json
cp "$CODEPROBE_KIT/templates/experiment.template.json" \
  .codeprobe/experiment.json
```

Initialization excludes local CodeProbe state from Git so trial worktrees start
from a clean checkout. Replace both `REPLACE_WITH_AGENT` values and both model
placeholders. Keep labels `A` then `B`, keep all controls symmetric, and change
only the dimension declared in the sampling plan. A model comparison changes
only `model`; a tooling comparison changes only the declared tooling fields.

The adapter-neutral template leaves `max_turns` unset. If the selected adapter
supports that capability, set the same positive cap on A and B. Otherwise leave
both values `null`; CodeProbe refuses unsupported knobs rather than silently
dropping them.

Run the evaluation in a data-owner-approved container or sandbox. Do not use
`--uncontained` for the pilot. Confirm agent credentials and the cost ceiling
locally; never send their values to provider support.

## 3. Mine the frozen sample

The default profile targets 15 candidates so predeclared attrition can still
leave ten paired tasks. Use the task goal and window frozen in the sampling
plan. This quality example is the default for supported repositories:

```bash
codeprobe mine . --goal quality --count 15 --no-interactive
```

If the plan declares another supported goal, replace only `quality`. Do not
change the window, task mix, selection method, or exclusions after inspecting
results. Record task and verifier SHA-256 digests locally in the sampling
worksheet; use anonymous `category_NN` identifiers.

## 4. Validate and inspect prompts locally

Validate every selected task and the complete experiment:

```bash
codeprobe validate .codeprobe/tasks --qa
codeprobe experiment validate .codeprobe
codeprobe run . --config .codeprobe --repeats 3 --show-prompt
```

The resolved prompt is prohibited data. Inspect it locally and do not paste,
stream, or summarize it to provider personnel. If validation removes or
quarantines tasks, record attrition. Stop with `insufficient_evidence` if fewer
than ten distinct paired tasks remain.

## 5. Dry-run, then execute both arms

Confirm the plan resolves to both configurations, the identical task set, and
three repeats:

```bash
codeprobe run . --config .codeprobe --repeats 3 --dry-run
```

Then run the exact approved plan:

```bash
codeprobe run . --config .codeprobe --repeats 3
codeprobe experiment status .codeprobe
codeprobe experiment aggregate .codeprobe
```

Keep `.codeprobe/runs/` and `.codeprobe/reports/aggregate.json` local. Confirm
that both arms have three scorable repeats for each of at least ten shared
tasks. Record failures and attrition honestly; never delete a failed trial,
repair evidence, or substitute a task after seeing results.

## 6. Build the local request

Copy
[`evidence-request.template.json`](templates/evidence-request.template.json)
to a private working path. Replace every `__...__` value using local facts:

- `codeprobe --version` for the exact runtime version;
- SHA-256 digests for configurations, tasks, and verifiers;
- the frozen sample dates, anonymous categories, exclusions, and attrition;
- aggregate quality, cost coverage, latency, uncertainty, and comparison
  values from local results;
- the coded events from
  [`intervention-log.json`](templates/intervention-log.json); and
- one bounded conclusion.

The template starts as `insufficient_evidence` with a refused comparison and
zeroed aggregates. Leaving those starter values is an honest insufficient
evidence outcome, not an advance decision.

## 7. Inspect, approve, and export

Preview writes nothing:

```bash
codeprobe snapshot evidence preview evidence-request.json --no-json
```

Review all five rendered artifacts against
[`bounded-findings.md`](templates/bounded-findings.md) and the four consent
statements: privacy, sample fidelity, result fidelity, and usefulness. If any
field is wrong or identifying, edit only the local request and preview again.
Never edit previewed artifacts.

Export only after approving the exact digest printed by the final preview:

```bash
codeprobe snapshot evidence export evidence-request.json \
  --out approved-evidence \
  --approve sha256:<digest-from-final-preview> \
  --no-json
```

The output directory must not already exist. Share only:

- `approved-evidence/run-manifest.json`
- `approved-evidence/sample-attestation.json`
- `approved-evidence/aggregate-results.json`
- `approved-evidence/findings.md`
- `approved-evidence/support-log.json`

## Recovery and refusal

- **Install or platform failure:** stop, retain details locally, and send only a
  sanitized coded status. An optional data-owner security/platform follow-up
  may occur without environment access.
- **Task or verifier defect:** record attrition. If the paired floor is lost,
  conclude `insufficient_evidence`; do not replace tasks after results.
- **CodeProbe defect:** stop the external run. Provider Engineering may fix and
  verify it internally; the data owner then starts a new external run.
- **Disqualifying support:** stop and record the exact coded event. The run
  cannot advance either configuration.
- **Changed request after preview:** discard the old digest, preview again, and
  repeat the full review.
- **Rejected or existing export path:** leave it untouched and choose a new,
  empty destination after resolving the cause.
