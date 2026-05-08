# codeprobe-jf28 — SDLC clean rerun (post-v0.10.0)

## Setup

5 SDLC tasks (`0d4ec3ad`, `45b581b5`, `ba1f3675`, `d906ac3d`,
`fde8e6e0`) × baseline + with-sg-fixed × N=3 = 30 trials. Run under
v0.10.0 with the codeprobe-emez fix in effect (`--config-parallel 1`
default), `--timeout 2700` (45 min, addressing the prior 30-min
truncations), `--max-cost-usd 60`.

Run dir: `~/test_repos/gascity/gascity-jf28-sdlc-clean/.codeprobe/`.

## Cost-cap behaviour: fixed

Total cost: $63.39 vs $60 cap = +$3.4 overshoot (about one
parallel-5 task's worth, well within expected per-task-completion
granularity).

Compare to `gascity-jf28-sdlc-rerun` (pre-v0.10.0): $58.33 vs $25 cap
= +$33.3 overshoot. **The codeprobe-emez fix is doing its job.**

## Quality data: contaminated by OAuth quota

Halfway through the with-sg-fixed config, the Claude Code OAuth
account hit its monthly usage limit. From that point on, each
`adapter.run()` returned a 41-byte error stub —
`"You've hit your org's monthly usage limit"` — instead of a real
agent output. The codeprobe adapter currently scores these as 0.0
without setting `error_category`, so `codeprobe interpret`'s
`mean_score = 0.000` for with-sg-fixed is misleading.

Filed separately as **codeprobe-9xrl** (P2): the adapter should mark
quota errors as `error_category="system"` and halt the run after
first detection. Until that lands, runs near the quota boundary will
silently contaminate aggregate stats.

### Real vs quota-error trial split

| Config | Real trials | Quota errors | Cost (real only, est.) |
|---|---|---|---|
| baseline | 15/15 | 0 | $33.55 |
| with-sg-fixed | 6/15 | 9 | ~$22 (rest is quota retry overhead) |

### baseline (15/15 real)

| Task | scores | mean |
|---|---|---|
| `0d4ec3ad` | [0, 0, 0] | 0.000 |
| `45b581b5` | [0.63, 0.63, 0.63] | 0.630 |
| `ba1f3675` | [0, 0, 0] | 0.000 |
| `d906ac3d` | [0.56, 0.56, 0.56] | 0.560 |
| `fde8e6e0` | [0, 0, 0.63] | 0.210 |

**baseline overall mean: 0.281** (matches gascity-jf28-sdlc-rerun's
baseline of 0.255 within noise — different real-trial count and
cache state explain the ~10% drift).

### with-sg-fixed (6/15 real, 9 quota)

| Task | real scores | real-only mean | covered |
|---|---|---|---|
| `0d4ec3ad` | [0, 0, 0] | 0.000 | full (3/3 ran) |
| `45b581b5` | [0, 0] | 0.000 | partial (2/3 ran) |
| `ba1f3675` | [0] | 0.000 | partial (1/3 ran) |
| `d906ac3d` | (none) | — | unmeasured (0/3 ran) |
| `fde8e6e0` | (none) | — | unmeasured (0/3 ran) |

**with-sg-fixed real-only mean: 0.000 (n=6)** — but the sample is
biased: the only tasks with multiple real runs are exactly the ones
where baseline also struggled (`0d4ec3ad`: baseline 0/3; `ba1f3675`:
baseline 0/3) and the one where baseline succeeded
(`45b581b5`: baseline 3/3). The two tasks where baseline did best
overall (`d906ac3d` 3/3 partial, `fde8e6e0` 1/3 partial) have zero
sg-fixed coverage.

## Within-task comparison (where we have data)

| Task | baseline | with-sg-fixed (real) | direction |
|---|---|---|---|
| `0d4ec3ad` | 0/3 | 0/3 | tied at fail |
| `45b581b5` | 3/3 partial @ 0.63 | 0/2 | **baseline wins** |
| `ba1f3675` | 0/3 | 0/1 | tied at fail (small n) |
| `d906ac3d` | 3/3 partial @ 0.56 | unmeasured | n/a |
| `fde8e6e0` | 1/3 partial @ 0.63 | unmeasured | n/a |

The prior SDLC rerun's "rescue pattern" (ba1f3675: baseline 0/3 →
sg-fixed 2/3 partial; fde8e6e0: baseline 0/2 → sg-fixed 1/3 partial)
**did not reproduce** in what we measured here — `ba1f3675` came
back 0/1 under sg-fixed instead of 2/3. But n=1 is not strong enough
to claim the prior result was an outlier; it's only enough to say
the rescue isn't robust.

The `45b581b5` regression (baseline 3/3 → sg-fixed 0/2) is more
reliable: 5 baseline trials at 0.63 vs 2 sg-fixed trials at 0.0,
across two reruns now. The v2 preamble appears to consistently hurt
this specific task.

## What this answers vs what's still open

Answers:

- **Cost cap holds under v0.10.0.** The codeprobe-emez fix is real
  and visible in the run telemetry.
- **The v2 preamble's mixed within-task signal on SDLC is at best
  fragile.** Even on the limited real-trial subset, the rescue
  pattern from the prior run didn't reproduce, and the regression on
  `45b581b5` did. Headline answer to "is v2 preamble better for
  SDLC?" is "still no, and possibly worse."

Doesn't answer:

- The full 5×2×3 SDLC matrix under v0.10.0 — would need the OAuth
  quota to reset (or to switch to API-key billing) plus the
  codeprobe-9xrl quota-detection fix to land so subsequent runs
  don't silently contaminate.
- Whether the `45b581b5` regression is intrinsic to the v2 preamble
  vs an artefact of the workflow_tail wording for SDLC. Worth
  inspecting the agent output on those trials to see what tool
  pattern differs from baseline's successful 0.63 runs.

## Followups (not action items now)

- **codeprobe-9xrl** — adapter quota-error detection (filed P2).
- A targeted N=5 rerun on just `45b581b5` and `ba1f3675` (the two
  tasks where prior signal was directional) would settle the v2
  preamble question with less budget than another full sweep.
  Estimated cost ~$30 if the quota-detection fix is in place.

## Reproducer

```bash
codeprobe run ~/test_repos/gascity/gascity-jf28-sdlc-clean/.codeprobe \
  --timeout 2700 --parallel 5 --repeats 3 --max-cost-usd 60 --force-plain
```

Run dir: `~/test_repos/gascity/gascity-jf28-sdlc-clean/.codeprobe/runs/`.
