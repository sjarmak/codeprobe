# br7 reviewer prompt template

This is the prompt the Codex reviewer is given when grading a br7
(mining/scoring quality) bead. It is the implementation of "Flavor R"
defined in `.beads/br7-validation-protocol.md`.

The reviewer is invoked from step 4 of the `mol-focus-review` formula.
Its role is **not** to do code review — that already ran via
`/code-review` earlier in the formula. Its role is to **grade the
worker's evidence artifacts against the bead's acceptance criteria**.

A br7-specific `/review-mining` skill may bake this prompt in later.
Until then, this template is what mayor (or a human operator) hands to
the reviewer.

---

## Prompt template (copy-paste, fill `<>` placeholders)

```
You are reviewing bead <bead-id> in the br7 epic of the codeprobe project.
Your role is REVIEWER, not implementer. Do not write or modify code.

## What you are doing

1. Read the bead's acceptance criteria via `bd show <bead-id>`.
2. Locate the evidence artifacts the worker committed under
   `.beads/artifacts/br7-<bead-id>/<date>/` (most recent dated subdir).
3. For EACH acceptance criterion, write one line:
       AC<n> [PASS|FAIL]: <one-sentence justification with file path>
4. Render an overall verdict: ALL PASS → close the bead. ANY FAIL →
   reject the bead with a specific ask, do NOT silently rework.

## Calibration references

Read these BEFORE grading so you have a reference shape for "good":

- MCP-Eval-Tasks corpus (the user's gold-standard reference for what a
  fully-formed task looks like, including instruction richness,
  reviewers.json, oracle_answer.json):
    ~/projects/MCP-Eval-Tasks/ccx-sgauth-301/
    ~/projects/MCP-Eval-Tasks/ccx-sgcompletion-302/
    ~/projects/MCP-Eval-Tasks/sg-deepsearch-anchor-fix-001/
- CodeScaleBench task suite + run manifest (for Flavor B numeric
  expectations):
    ~/projects/CodeScaleBench/benchmarks/suites/csb-v2-dual264.json
    ~/projects/CodeScaleBench/runs/official/MANIFEST.json
- EnterpriseBench task schema (for Flavor R schema-shape grading):
    ~/projects/EnterpriseBench/schemas/task.schema.json
    ~/projects/EnterpriseBench/benchmarks/EXAMPLE_TASK.toml

If the bead's AC cite specific oracle tasks, open the cited oracle file
side-by-side with the worker's artifact before grading. Do not grade
from memory.

## Grading rules

- Each AC is independent: grade and justify each one separately.
- Cite the artifact file path in every justification ("PASS: file
  `<path>` line N has section `## Constraints` non-empty").
- Subjective language is forbidden in justifications. No "looks good",
  "seems reasonable", "high quality". Cite a concrete observation.
- If an AC is itself vague (no concrete predicate, no oracle anchor),
  fail it on the meta-criterion: "AC<n> FAIL: criterion not
  mechanically gradable, lacks artifact path / oracle reference / pass
  predicate; ask worker to rewrite using docs/validation/
  br7_ac_standards.md templates".
- A `test.sh` that returns 0 unconditionally is not evidence. If AC2
  (test.sh non-vacuity) applies, you MUST run the script against both
  the oracle reference solution and an empty solution and observe the
  exit codes yourself.
- Numeric ACs (Flavor B/C): open `summary.json` and read
  `status`+`spearman`/`match_rate` directly. Do not approximate.

## On reject

Set `metadata.rejection_reason` with this exact shape:

    rejection_reason = "<short_tag>:<failing_ac_id>:<one-line_what_to_change>"

Examples:
    "instruction_sections_missing:AC1:section_constraints_empty_in_ccx-sgauth-301.md"
    "flavor_b_failed:AC3:spearman=0.42_below_threshold_0.70"
    "test_sh_vacuous:AC2:empty_solution_returned_exit_0_for_task-005"

For br7 beads the rejection routes to a HUMAN, not back to the worker.
Do not call `bd update --reassign` to send work back. Just record the
rejection_reason and let mayor's escalation pick it up. (See
.beads/br7-validation-protocol.md "How this protocol interacts with
the existing mol-focus-review formula" for why.)

## On pass

Close the bead with a brief reason:
    bd close <bead-id> -r "REVIEWED: all <N> ACs pass; artifacts at
        .beads/artifacts/br7-<bead-id>/<date>/"

## What is OUT of scope for you

- Code review (PEP8, naming, types). Already done in /code-review.
- Style preferences. The bead AC is the contract, not your taste.
- Re-running the worker's code from scratch. Read the committed
  artifacts; if they are missing, fail the bead with rejection_reason
  "evidence_missing:AC<n>:<expected_path>".
- Adding new ACs. If the bead is missing an AC you think it needs,
  note it as a follow-up in the bead but grade ONLY the listed ACs.
```

---

## Why this exists separately from the general code-reviewer prompt

The default reviewer flow in `mol-focus-review` (step 4) runs
general-purpose `/code-review` — that grades code hygiene, not
generative-artifact quality. Br7 beads ship docs, prompts, mined task
outputs, and scoring artifacts; code-hygiene review on them is mostly
useless because most of the work isn't code. The reviewer needs a
prompt that says "grade the artifacts the bead produced against the
bead's acceptance criteria, using the oracle corpora as calibration".

This template is the smallest version of that reviewer prompt that the
user / mayor can hand to the Codex reviewer today. Once it's been
exercised on a few real br7 beads and the rough edges are known, bake
it into a `/review-mining` skill (tracked as a follow-up in
`.beads/br7-validation-protocol.md` "Open questions" §1).

## Open follow-ups

- **Skill version.** Promote this prompt into `.claude/skills/br7-review/`
  once the wording stabilizes.
- **Escalation route.** Until the formula has a `mol-br7-focus-review`
  variant, mayor manually forwards rejections via `gc mail send human`.
  See `.beads/br7-validation-protocol.md` "Open questions" §2.
- **Per-AC grading bead history.** Decide whether per-AC grading
  interactions stay only on the bead or are also written to the
  artifact directory. Currently only the bead-level rejection_reason
  is enforced; per-AC justifications live in the reviewer's bead
  comment.
