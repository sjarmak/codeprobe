# br7 acceptance-criteria standards

This document defines the AC writing standard for every br7 (mining /
scoring quality) bead. The br7 validation protocol
(`.beads/br7-validation-protocol.md`) puts the *primary gate* on
**Flavor R — reviewer grading**: the Codex reviewer reads the bead's
acceptance criteria and grades each one against committed artifacts.
That gate works only if each AC is **concrete, oracle-anchored, and
mechanically inspectable**.

This is the cure for the failure mode that produced this protocol in
the first place: vague ACs ("instructions should be high quality",
"output should be good") that worker self-grade as green and reviewers
have no way to refute.

## What a good br7 AC looks like

Every br7 AC must satisfy all four of:

1. **Concrete artifact path.** Names the file/directory the reviewer
   must inspect. No "the output", no "the result". Example:
   `.beads/artifacts/br7-<id>/<date>/instructions/CCX-sgauth-301.md`.
2. **Anchored to an oracle reference.** Either points at a real oracle
   task (e.g. `~/projects/MCP-Eval-Tasks/ccx-sgauth-301/instruction.md`)
   or at a deterministic computed metric (e.g. Spearman correlation
   from `flavor_b_score_correlation`).
3. **Pass/fail predicate the reviewer can apply.** Specifies the bar:
   "section X exists", "uses ≥ N of these constructs", "match rate ≥ 0.8".
   Avoids subjective language like "high-quality", "well-structured".
4. **Failure mode named.** What the reviewer should write in
   `metadata.rejection_reason` if this AC fails. Example: "section
   `## Constraints` missing → ask worker to add".

## Anti-patterns to reject during bead intake

- "Instructions should be detailed and clear." — no predicate.
- "The mining yield should improve." — no measurement, no baseline.
- "Tests should pass." — vacuous on generative work; the worker can
  emit a `test.sh` that returns 0 unconditionally.
- "Should match CodeScaleBench format." — no specific schema field
  list, no specific oracle task referenced.
- "Add ≥ 6 features to the prompt." — no way to verify which features
  ended up in the artifact.

## Six concrete AC templates

Each template is meant to be copy-pasted into a br7 bead description and
filled in. The placeholders are uppercase in `<>`.

### Template 1 — Instruction richness vs an oracle task (Flavor R)

> **AC1 (instruction richness).** For each task in
> `<artifact_dir>/instructions/`, the file MUST contain the same
> top-level sections as the corresponding oracle reference at
> `~/projects/MCP-Eval-Tasks/<oracle_task_id>/instruction.md`:
> `## Context`, `## Goal`, `## Constraints`, `## Acceptance criteria`.
> Each section MUST be non-empty and ≥ 3 lines. Reviewer: open both
> files side by side; reject if any section is missing or stub-length.
> On fail set `metadata.rejection_reason = "instruction_sections_missing:
> <task_id>:<section_name>"`.

### Template 2 — Test.sh non-vacuity (Flavor R)

> **AC2 (test.sh discriminates good vs broken).** For each
> `<artifact_dir>/tasks/<task_id>/tests/test.sh`, run it twice:
> once with the oracle reference solution at
> `~/projects/MCP-Eval-Tasks/<oracle_task_id>/tests/oracle_answer.json`
> applied as the agent output, and once with an empty agent output.
> The first run MUST exit 0; the second MUST exit non-zero. Reviewer:
> commit both run logs as `runs/good.log` and `runs/empty.log`. On
> either side failing the expected exit code, set
> `metadata.rejection_reason = "test_sh_vacuous:<task_id>:<which_run>"`.

### Template 3 — Score correlation against CSB (Flavor B)

> **AC3 (score correlation).** Produce paired scores via the Python API
> (this module ships no CLI; a thin runner script lives at
> `<artifact_dir>/run_flavor_b.py` that invokes
> `codeprobe.assess.oracle_diff.flavor_b_from_csb_manifest(...)` then
> `flavor_b_score_correlation(paired_scores_csv=<artifact_dir>/paired_scores.csv,
> min_correlation=0.70, artifact_dir=<artifact_dir>/flavor_b/)` against ≥
> 20 paired tasks). Source: codeprobe scores vs CSB run-manifest reward
> from `~/projects/CodeScaleBench/runs/official/MANIFEST.json`. The
> committed `<artifact_dir>/flavor_b/summary.json` MUST report
> `status: pass` and `n_tasks ≥ 20`. Reviewer: open
> `flavor_b/correlation.json` and `outliers.md`; if `spearman < 0.70`
> or `n_tasks < 20`, set
> `metadata.rejection_reason = "flavor_b_failed:spearman=<value>"`.

### Template 4 — E2E pipeline divergence (Flavor C)

> **AC4 (E2E divergence ≤ 20%).** On the same ≥ 5 tasks, run the agent
> through codeprobe AND through the oracle harness; commit
> `<artifact_dir>/codeprobe_outcomes.json` and
> `<artifact_dir>/oracle_outcomes.json`. Then run
> `flavor_c_e2e_divergence(...)` with `min_match_rate=0.8`. The
> resulting `summary.json` MUST report `status: pass` and the CSV
> `e2e_outcomes.csv` MUST list every joined task. Reviewer: confirm
> `n_tasks ≥ 5`; on fail set `metadata.rejection_reason =
> "flavor_c_failed:match_rate=<value>"`.

### Template 5 — Schema parity with EnterpriseBench (Flavor R)

> **AC5 (task.toml shape parity with EB).** For each task at
> `<artifact_dir>/tasks/<task_id>/task.toml`, every top-level table
> required by `~/projects/EnterpriseBench/schemas/task.schema.json`
> MUST be present (`[task]`, `[[repos]]`, `[metadata]`,
> `[[checkpoints]]`). Each `[[checkpoints]]` MUST have `name`,
> `weight`, `verifier`, `description`, `timeout_seconds`. Reviewer:
> diff each emitted `task.toml` against
> `~/projects/EnterpriseBench/benchmarks/EXAMPLE_TASK.toml` for table
> presence. On fail set `metadata.rejection_reason = "task_toml_shape:
> <task_id>:<missing_table>"`.

### Template 6 — Mining yield baseline + delta (Flavor R + numeric)

> **AC6 (mining yield improvement).** Commit
> `<artifact_dir>/mining_yield.json` with shape
> `{repo, n_prs_scanned, n_tasks_extracted, yield_pct, baseline_yield_pct,
> delta_pct}`. Run codeprobe mining against `<repo>` (e.g.
> `kubernetes/kubernetes`); the new `yield_pct` MUST be ≥
> `baseline_yield_pct + 5.0` (absolute percentage points). Baseline
> comes from the most recent committed `mining_yield.json` for the same
> repo under `.beads/artifacts/br7-*/`; if none, the bead must commit a
> new baseline run BEFORE the change and reference it. Reviewer:
> compare the two committed JSON files; on fail set
> `metadata.rejection_reason = "mining_yield_no_improvement:
> delta=<value>"`.

## Composing ACs

Most br7 beads will combine 2–3 of these templates. A typical scoring
bead like br7.3 (weighted checklist `test.sh`) would use AC1
(instruction richness) + AC2 (test.sh non-vacuity) + AC3 (Flavor B
correlation). A pipeline bead like br7.6 would use AC4 (Flavor C) plus
AC2 on a sampled subset.

## When to add a new template

If a br7 bead needs an AC that none of these templates fits, the bead
description MUST add a new template inline AND propose adding it to
this document in the bead's close interaction. Do not invent
ad-hoc one-off AC patterns — that's how the original failure mode
returned.

## Reference fixtures

These are the fixtures that templates above point at:

- **MCP-Eval-Tasks (CCX/SG corpus, Flavor R primary):**
  `~/projects/MCP-Eval-Tasks/ccx-sgauth-301/`,
  `~/projects/MCP-Eval-Tasks/ccx-sgcompletion-302/`,
  `~/projects/MCP-Eval-Tasks/ccx-sgencrypt-305/`,
  `~/projects/MCP-Eval-Tasks/sg-deepsearch-anchor-fix-001/`,
  `~/projects/MCP-Eval-Tasks/sg-deepsearch-imgbomb-fix-001/`,
  `~/projects/MCP-Eval-Tasks/sg-gitlab-ratelimit-fix-001/`.
  Each contains `task.toml`, `instruction.md`, `instruction_mcp.md`,
  `reviewers.json`, `environment/`, and `tests/{test.sh, eval.sh,
  expected/, oracle_answer.json}`.
- **CodeScaleBench (Flavor B paired-score source):**
  `~/projects/CodeScaleBench/runs/official/MANIFEST.json` (183 runs ×
  2780 tasks; `runs[<run_key>].tasks[<task_id>].reward`).
- **EnterpriseBench (Flavor R schema source):**
  `~/projects/EnterpriseBench/benchmarks/EXAMPLE_TASK.toml` and
  `~/projects/EnterpriseBench/schemas/task.schema.json`.

Keep these paths verbatim; bead AC text that drifts from these exact
paths is the "stale-path" failure mode the protocol forbids
(`.beads/br7-validation-protocol.md` "What does NOT count as evidence").
