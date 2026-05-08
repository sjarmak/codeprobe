# codeprobe-jf28 — SDLC family rerun (unsaturated rewards check)

## Question

The 1-rep 3-way and the ttwq oracle_checks rerun both produced
saturated rewards (1.0 across all configs). To learn whether the v2
preamble actually changes quality (not just cost), we need a category
where rewards don't ceiling out. SDLC is the obvious target — past
runs (`codeprobe-mcn7`, `codeprobe-3oms`) showed real variance in the
+0.054 / noise range, with means well below 1.0.

This rerun replicates `mcn7`'s shape: 5 SDLC tasks (`0d4ec3ad`,
`45b581b5`, `ba1f3675`, `d906ac3d`, `fde8e6e0`) × 2 configs (baseline,
`with-sg-fixed`) × N=3. We use `with-sg-fixed` rather than
`with-sg-isolated` because SDLC tasks need source files for the agent
to edit; file-removal isolation is structurally incompatible with
code-edit verification.

The intervention under test: the v2 sourcegraph preamble alone
(no isolation). Endpoint is `/all`. Compared against `with-sourcegraph`
in `mcn7` (v1 preamble, `/v1` endpoint).

## Result

**Rewards are genuinely unsaturated (range 0.0–0.63, no perfects), but
the v2 preamble does not move the SDLC mean.** The within-task signal
is mixed — v2 rescues some tasks but hurts others.

| Task | baseline (3 reps) | with-sg-fixed (3 reps) | mean δ |
|---|---|---|---|
| `0d4ec3ad` | [0, 0, 0] | [0, 0, 0] | 0.00 |
| `45b581b5` | [0.63, 0.63, 0.63] | [0, 0.63, 0] | **−0.42** |
| `ba1f3675` | [0, 0, 0] | [0, 0.59, 0.59] | **+0.39** |
| `d906ac3d` | [0.56, 0.56, 0.56] | [0.56, 0.56, timeout] | −0.19 |
| `fde8e6e0` | [0, 0, *missing*] | [0, timeout, 0.63] | +0.21 |

Aggregate (treating timeouts as 0 score):

- per-task average: baseline 0.238 vs with-sg-fixed 0.238 — **tied**
- per-trial mean: baseline 0.255 vs with-sg-fixed 0.237 — baseline
  +0.018 (well within noise)
- baseline perfect-rate: 0/14 (no 1.0 trials)
- with-sg-fixed perfect-rate: 0/15

Compare to `mcn7`'s family-level finding: "+0.054 nominal,
collapses to noise at N=3." This rerun reproduces the "noise" half
of that finding. The +0.054 direction is gone; sign flips depending
on per-task vs per-trial weighting.

## What the within-task pattern tells us

- **v2 rescues** `ba1f3675` (baseline always 0, sg-fixed 2/3 partial)
  and `fde8e6e0` (baseline always 0, sg-fixed 1/3 partial). On these,
  the agent finds a path through that local Read/Grep doesn't.
- **v2 hurts** `45b581b5` (baseline always 0.63, sg-fixed 1/3 partial).
  Looking at the per-trial scores, the failures are wholesale
  (0.0 vs the achievable 0.63), suggesting the agent went down a
  wrong path entirely.
- **Tied at fail** on `0d4ec3ad` (both 0/3). Neither approach finds
  the right structure — this task is hard for both.
- **Roughly tied** on `d906ac3d` — sg-fixed matches baseline on 2/3
  trials and times out on the third.

The "shifts variance between tasks" reading is consistent with the
preamble change: v2 makes the agent commit to an MCP plan earlier,
which is a win when the local-only path was missing context, and a
loss when the MCP plan locks in on a wrong file early.

## Caveats

### Cost cap was overrun in the original run

`gascity-jf28-sdlc-rerun` ran `$58.33` against a `$25` cap, and only
completed 23/30 trials before being throttled. The 6 missing
with-sg-fixed trials (`d906ac3d` × 3 and `fde8e6e0` × 3) were
recovered by `gascity-jf28-sdlc-catchup` (separate run, same task
metadata, `--parallel 3 --max-cost-usd 25`). The catchup completed
4 successfully + 2 timeouts. 1 baseline trial on `fde8e6e0` is still
missing — to fully balance the 5×2×3 matrix you'd need to run that
one trial separately. Filed as `codeprobe-emez` (P2 bug).

### 2 with-sg-fixed trials hit the 30-min timeout

`d906ac3d` rep 3 and `fde8e6e0` rep 2 ran for the full 1800-s
timeout without producing a complete answer; they're scored 0 by
default (envelope shows `cost_usd: null` because the harness killed
the process before billing closed out). This is a real cost of
running v2 preamble on SDLC — the agent burns more wall-clock
exploring the codebase via MCP. A `--timeout 2700` rerun would tell
us whether those 0-scores are intrinsic failures or just budget
exhaustion.

### Per-trial cost is high for SDLC

- baseline: ~$2 per trial average
- with-sg-fixed: ~$3 per trial average (50% more)

For an N=3 5-task SDLC sweep, expect $25-30 baseline and $30-40
sg-fixed. Plan accordingly.

## What this answers (and what it doesn't)

Answers:

- **Saturation isn't the only story.** SDLC produces real variance
  (range 0.0–0.63, no perfects) so the saturated oracle_checks runs
  weren't a property of the comparison framework — they were a
  property of those tasks.
- **The v2 preamble doesn't improve SDLC quality on average.**
  Per-task mean tied; per-trial mean nominally baseline-favoured.
  This matches `codeprobe-mcn7`'s noise finding.

Doesn't answer:

- Whether v2 with `--timeout 2700` (no premature cuts) would shift
  the result. The 2 timeouts represent ~13% of with-sg-fixed trials.
- Whether the v2 preamble's mixed within-task pattern is meaningful
  signal (rescue some, hurt others) vs single-trial-per-task noise.
  N=10 per task would tighten this; cost ~$80-100.
- Whether other SDLC tasks (codeprobe-mcn7 ran across more) would
  show different behaviour.

## Reproducer

```bash
# Original SDLC rerun (overran cost cap; partial)
codeprobe run ~/test_repos/gascity/gascity-jf28-sdlc-rerun/.codeprobe \
  --timeout 1800 --parallel 5 --repeats 3 --max-cost-usd 25 --force-plain

# Catchup for missing with-sg-fixed trials
codeprobe run ~/test_repos/gascity/gascity-jf28-sdlc-catchup/.codeprobe \
  --timeout 1800 --parallel 3 --repeats 3 --max-cost-usd 25 --force-plain
```

Run dirs:
- `~/test_repos/gascity/gascity-jf28-sdlc-rerun/.codeprobe/runs/`
- `~/test_repos/gascity/gascity-jf28-sdlc-catchup/.codeprobe/runs/`

## Related

- Bug `codeprobe-emez` — `--max-cost-usd` overshoots when configs
  run in parallel. Filed as P2.
- Predecessor: `codeprobe-mcn7` — original SDLC-at-N=3 finding.
- Predecessor: `codeprobe-3oms` — first observation of "+0.054 nominal."
