# codeprobe-9tk — STEP 0: per-trial wall-clock guard raise

**Status:** landed (precondition only — no trials run yet)
**Bead:** `codeprobe-9tk` (`[44i-confirm]` with-sg-narrow SDLC confirm run)
**Branch:** `codeprobe-9tk-44i-confirm`

## Decision

**Set `extra.timeout_seconds = 10800` (3 h) on every arm of the 9tk run.**

This is the mandatory STEP-0 precondition from the bead NOTES: raise the
per-trial wall-clock guard *before* any 9tk trial fires, sized so the hardest
task (`0d4ec3ad`) is bounded by *turns* (now uncapped), not by the clock.

`timeout_seconds` is the only wall-clock guard in the stack: it is the
per-task subprocess timeout the executor passes to the adapter
(`AgentConfig.timeout_seconds`, default `3600`). There is **no** other
code-level wall-clock clamp (verified: no `5400`/timeout-max in
`adapters/claude.py` or `core/executor.py`). So "raise the guard" = raise this
config value on every arm's experiment config; there is no source-level
constant to bump.

## Why 5400 is proven too low

The uncapped 4cl6.3 sweep ran with `timeout_seconds = 5400` (90 min) and the
guard **did** bind on the hardest task. From
`docs/investigations/codeprobe-4cl6/uncapped.md` (per-trial detail):

| task | repeat | result | turns | wall-clock |
|------|--------|--------|-------|------------|
| `0d4ec3ad` | 0 | success (0.827) | 171 | 2800s |
| `0d4ec3ad` | 1 | success (0.828) | 180 | 2592s |
| `0d4ec3ad` | 2 | **wall-clock timeout (0.000)** | — | **5400s** (clipped) |

`0d4ec3ad` r2 stored `status='error'` with only `task_time_seconds=5400.12`
and no cost/token/turn telemetry — the signature of a 90-minute wall-clock
kill, not a model error. Its true completion time is unknown (`> 5400s`). That
single clipped trial is the lone 0.0 in the uncapped arm and drags
`0d4ec3ad`'s per-task mean to 0.551 (vs mcn7 with-sg's ~0.80) — i.e. the
*clock*, not turns, became the binding constraint once turns were unbounded.

Re-running 9tk at 5400 would reproduce that artifact and confound the
narrow-vs-full-vs-local comparison the bead exists to make.

## Sizing rationale

Anchored on `0d4ec3ad`'s observed uncapped behaviour plus margin:

- Successful `0d4ec3ad` trajectories complete in **≤ 2800s** at 171–180 turns
  (~15 s/turn).
- The failing r2 was still running at the **5400s** guard → its real need is
  `> 5400s`, magnitude unknown.
- The longest *successful* trial anywhere in the uncapped arm was **4332s**
  (`ba1f3675` r0, 153 turns).

**10800s = 2× the clip point (5400) and ~2.5× the longest observed successful
trial (4332s).** That makes the wall-clock effectively non-binding for the
observed distribution while still bounding a pathological hang. Dollar spend
stays bounded independently by the run's `--max-cost-usd` budget (~$120), so a
generous wall-clock guard costs nothing on well-behaved trials — it only
removes the premature-kill failure mode on the fat right tail.

The guard is deliberately *not* set from `0d4ec3ad`'s successful max alone
(2800s): that anchor is below the prior guard and would re-clip the exact
trial this raise exists to protect.

## Application checklist (for the deferred full run)

When the 3-arm experiment is built (bead steps 1–6, currently paused):

- [ ] `with-sg-narrow` config: `extra.timeout_seconds = 10800`
- [ ] `with-sg-full` config: `extra.timeout_seconds = 10800`
- [ ] `local-only` config: `extra.timeout_seconds = 10800`
- [ ] All arms uncapped (`max_turns = null` / omit `--max-turns`) per the 4cl6
      recommendation in the bead NOTES.
- [ ] Re-confirm `error_max_turns` rate ~0 (acceptance A2) and that no trial
      terminates on `task_time_seconds ≈ 10800` (the new clip signature).

## Scope of this commit

STEP 0 only. This commit pins and justifies the guard value on the run branch.
The full config recovery (`with-sg-narrow` tool surface from evjr.4 commit
`462b3a6`), the 90-trial run, analysis, and writeup are **not** done here —
they remain deferred pending explicit go-ahead (the run is ~$120 real spend,
multi-hour, QUOTABLE-EXTERNAL).
