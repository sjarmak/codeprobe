# codeprobe-jf28 — SDLC rerun v2 (clean data, post-9xrl)

## Setup

5 SDLC tasks (`0d4ec3ad`, `45b581b5`, `ba1f3675`, `d906ac3d`,
`fde8e6e0`) × baseline + with-sg-fixed × N=3 = 30 trials. Run under
codeprobe v0.10.1 (codeprobe-9xrl quota detection in place) on a
fresh OAuth account. `--timeout 2700`, `--parallel 5`,
`--config-parallel 1` (default), `--max-cost-usd 60`.

Run dir: `~/test_repos/gascity/gascity-jf28-sdlc-v2/.codeprobe/`.

## Result

**The v2 sourcegraph preamble underperforms baseline on SDLC by
−0.087 mean reward, with no quota contamination this time.** All 30
trials completed cleanly.

| Config | n | mean | perfect | total cost | mean duration |
|---|---|---|---|---|---|
| baseline | 15 | **0.242** | 0/15 | $32.52 | 497 s |
| with-sg-fixed | 15 | **0.155** | 0/15 | $54.62 | 1494 s |

baseline 95% CI: [0.09, 0.40]; with-sg-fixed CI: [0.02, 0.29]. Not
significant at p=0.05 (single repeat-set; CIs overlap). But the
within-task pattern is consistent.

### Per-task

| Task | baseline scores | sg-fixed scores | Δ (sg − base) |
|---|---|---|---|
| `0d4ec3ad` | [0, 0, 0] → 0.00 | [0, 0, 0] → 0.00 | 0.00 |
| `45b581b5` | [0.63, 0.63, 0.63] → 0.63 | [0, 0, 0] → 0.00 | **−0.63** |
| `ba1f3675` | [0, 0.61, 0] → 0.20 | [0, 0, 0] → 0.00 | **−0.20** |
| `d906ac3d` | [0.56, 0.56, 0] → 0.37 | [0.56, 0.56, 0.56] → 0.56 | **+0.19** |
| `fde8e6e0` | [0, 0, 0] → 0.00 | [0, 0, 0.63] → 0.21 | **+0.21** |

Magnitude check: losses (−0.63 + −0.20 = −0.83) vs wins
(+0.19 + +0.21 = +0.40). The net effect is dominated by the
`45b581b5` regression where baseline reliably scored 3/3 partial @
0.63 and sg-fixed went to 0/3 across the board.

## What's interesting about the pattern

**With-sg-fixed rescues some tasks where baseline always fails:**

- `fde8e6e0`: baseline 0/3, sg-fixed 1/3 partial. The agent under v2
  preamble found a path through Sourcegraph that local Read/Grep
  missed.
- `d906ac3d`: baseline 2/3 partial, sg-fixed 3/3 partial. Sg-fixed
  picks up the trial that baseline failed on.

**With-sg-fixed loses big on tasks where baseline reliably scores:**

- `45b581b5`: 3/3 → 0/3. Baseline consistently gets 0.63 partial; sg-
  fixed lands 0.0 on every trial despite running 4–8× longer
  wall-clock.
- `ba1f3675`: 1/3 → 0/3. Smaller magnitude but same direction.

**Wall-clock cost is meaningful.** sg-fixed mean duration 1494 s vs
baseline 497 s (3× longer). On `45b581b5` specifically, sg-fixed
trials ran 1453, 1507, and **2568 s** — the third one was within
130 s of the 2700 s timeout. Agents under v2 preamble are spending
substantially more time exploring via Sourcegraph and still failing
to produce a working edit on the tasks they were already losing on.

This is consistent with the pattern from earlier rigs: MCP-augmented
agents tend to over-explore the codebase before committing to an
edit, and on SDLC tasks the right strategy is "find the file, edit
it, run tests" — not "trace every reference first."

## Cost-cap overshoot

Total cost $87.14 vs $60 cap = +$27 overshoot. This is
within-config overshoot (`--parallel 5` × ~$5 per SDLC task = $25
in-flight when the cap fires). The codeprobe-emez fix addresses
cross-config dispatch; within-config overshoot is still bounded by
`parallel × per_task_cost`. To tighten it you'd either drop
`--parallel` to 1 (slow), reduce `--max-cost-usd` to absorb the
expected overshoot, or pre-authorize budget at task-start (a real
fix that doesn't exist yet).

Not a regression vs the prior SDLC rerun ($58 vs $25 = +$33). Worth
mentioning in `docs/agent_config.md` so users plan their cap
accordingly.

## Quota detection: dormant but verified

The codeprobe-9xrl detection was loaded and active throughout the
run. No `error_category="quota"` entries appeared in the results,
which is the correct null-result for an account that didn't hit a
limit. Sanity-checked via the unit tests (18/18 passed locally
before launch).

## Headline answer to "is the v2 preamble better for SDLC?"

**No.** With clean data, the v2 sourcegraph preamble underperforms
baseline by −0.087 mean reward on this 5-task SDLC subset, costs 68%
more, and runs 3× longer. The mixed within-task pattern (rescues
some, loses others) hasn't fully resolved into a clear win pattern
across three reruns — it's noisy enough that the per-task signs
balance, but the magnitudes don't: losses on tasks baseline can
solve are larger than wins on tasks baseline can't.

This **doesn't** invalidate the v2 preamble for non-SDLC families:

- Oracle_checks (jf28 ttwq rerun): the v2 preamble + sg-only
  isolation **fixes** the oc_004 regression that v1 had (15/15
  perfect vs v1's 12/15 with the FlagAliases denial).
- Symbol-reference-trace and change-scope-audit families haven't
  been re-evaluated under v2; expected upside there given the
  preamble's explicit `sg_find_references` authority.

The takeaway: **use the v2 preamble for retrieval/oracle/text-answer
tasks; do not use it for SDLC code-edit tasks.** The
`with-sg-isolated` config (file-removal mode) doesn't even apply to
SDLC because the agent needs files to edit, so the sg-only path
isn't available there.

## Followups

- Document the SDLC-specific guidance in the `experiment` skill so
  future agents pick the right config per task family
  (oracle/symbol-ref → with-sg-isolated; SDLC → baseline).
- A focused N=10 rerun on just `45b581b5` (the most consistent
  regression) would tighten the magnitude estimate. ~$30 budget if
  done with `--parallel 5 --config-parallel 1`.
- Compare agent_output.txt traces between baseline (consistent
  partial pass) and sg-fixed (consistent fail) on `45b581b5` to
  identify what the v2 preamble steers the agent toward that breaks
  the working edit pattern. Likely worth a separate investigation
  bead.

## Reproducer

```bash
codeprobe run ~/test_repos/gascity/gascity-jf28-sdlc-v2/.codeprobe \
  --timeout 2700 --parallel 5 --repeats 3 --max-cost-usd 60 --force-plain
codeprobe interpret ~/test_repos/gascity/gascity-jf28-sdlc-v2/.codeprobe
```
