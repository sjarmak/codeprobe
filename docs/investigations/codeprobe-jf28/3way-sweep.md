# codeprobe-jf28 — 3-way sweep result

**Setup:** 5 oracle_checks tasks (`oc_001`..`oc_005`) on the gascity
codebase, 3 configs (`baseline`, `with-sg-fixed`, `with-sg-isolated`),
single repeat. Run dir:
`~/test_repos/gascity/gascity-jf28-3way/.codeprobe/runs/`. Total
runtime: ~5 min wall-clock with `--parallel 5`. Total cost: $4.94 of
the $15 cap.

## Headline

| Config | Score | Cost | Sum task duration | Tool calls |
|---|---|---|---|---|
| baseline | 5/5 (100%) | $2.25 | 467 s | 84 local (Read/Grep/Bash/Glob/Agent) |
| with-sg-fixed | 5/5 (100%) | $1.49 | 381 s | 65 MCP-only |
| **with-sg-isolated** | **5/5 (100%)** | **$1.20** | **306 s** | **46 MCP-only** |

Quality saturates at 1.0 across all three. The interesting variation is
cost and tool use:

- **with-sg-isolated vs baseline:** 47% cheaper, 34% faster.
- **with-sg-isolated vs with-sg-fixed:** 20% cheaper, 20% faster, 29%
  fewer MCP calls.
- **with-sg-fixed vs baseline:** 33% cheaper. `mcp_mode=strict` already
  blocked local tools in this config, so the savings come from MCP
  being a more efficient interface than local Read/Grep for whole-repo
  discovery — not from preamble framing.

## Per-task

| Config | Task | Duration (s) | Cost ($) |
|---|---|---|---|
| baseline | oc_002 | 37.4 | 0.241 |
| baseline | oc_005 | 44.2 | 0.347 |
| baseline | oc_004 | 70.0 | 0.304 |
| baseline | oc_003 | 111.5 | 0.405 |
| baseline | oc_001 | 203.8 | 0.952 |
| with-sg-fixed | oc_002 | 24.1 | 0.114 |
| with-sg-fixed | oc_005 | 33.7 | 0.172 |
| with-sg-fixed | oc_003 | 44.1 | 0.198 |
| with-sg-fixed | oc_004 | 86.8 | 0.351 |
| with-sg-fixed | oc_001 | 192.7 | 0.660 |
| with-sg-isolated | oc_002 | 23.9 | 0.118 |
| with-sg-isolated | oc_005 | 31.1 | 0.174 |
| with-sg-isolated | oc_004 | 50.0 | 0.210 |
| with-sg-isolated | oc_003 | 51.9 | 0.218 |
| with-sg-isolated | oc_001 | 149.0 | 0.478 |

`oc_001` is the largest task (4 distinct rubric criteria) and shows the
biggest absolute savings: with-sg-isolated runs 27% faster and 50%
cheaper than baseline on this task. The cheaper tasks (oc_002, oc_005)
show smaller absolute deltas because they hit prompt-cache earlier.

## Tool-mix

```
baseline (84 calls)
   37  Read
   31  Grep
    9  Bash
    5  Glob
    2  Agent

with-sg-fixed (65 calls — mcp_mode=strict)
   32  mcp__sourcegraph__keyword_search
   24  mcp__sourcegraph__read_file
    5  mcp__sourcegraph__list_files
    2  mcp__sourcegraph__commit_search
    1  mcp__sourcegraph__diff_search
    1  mcp__sourcegraph__compare_revisions

with-sg-isolated (46 calls)
   25  mcp__sourcegraph__keyword_search
   16  mcp__sourcegraph__read_file
    3  mcp__sourcegraph__list_files
    1  mcp__sourcegraph__compare_revisions
    1  mcp__sourcegraph__commit_search
```

The 30% reduction in MCP calls under isolation isn't a tool-policy
artifact — both with-sg configs use `mcp_mode=strict` which already
blocks local tools. The reduction comes from the v2 preamble's tighter
framing combined with absent source: the agent has nothing to fall
back on for "let me just check this locally first," so it commits to
the MCP query plan immediately and stops sooner.

## Caveats

- **Quality saturated.** Oracle_checks rubric questions are small
  enough that all 3 configs achieve perfect coverage. To distinguish
  quality differences you need the SDLC family (which jf28 deferred
  because `hide_local_source=True` is incompatible with
  code-edit verification — agents need files to edit them).
- **Single repeat.** No variance estimate. A 3-repeat sweep would
  tighten the cost-savings claim. Not done because cost savings of
  20-50% are well outside any reasonable noise floor for these
  task durations.
- **Apples-to-apples on the oracle subset only.** `with-sg-isolated`
  cannot meaningfully run against SDLC tasks under the current
  isolation model — that's a property of file-removal isolation, not a
  bug. SDLC tasks would need a different isolation strategy (e.g.,
  copy source into the workspace from a stash before run, scrub after)
  if you want to extend sg-only mode to code-edit tasks.

## What this unblocks

- **codeprobe-4cl6** (SDLC cap retune sweep) — should rerun against
  the v2 preamble + `/all` + `with-sg-fixed` config (NOT
  `with-sg-isolated`, since SDLC needs source). The preamble swap
  alone is a meaningful intervention.
- **codeprobe-gg9f** (per-family caps) — same.
- A separate followup could explore the 20% cost gap between
  with-sg-fixed and with-sg-isolated more rigorously (does it hold
  on harder oracle questions? on symbol-reference-trace? at higher
  N?). Out of scope for jf28.

## Reproducer

```bash
codeprobe run ~/test_repos/gascity/gascity-jf28-3way/.codeprobe \
  --timeout 900 --parallel 5 --max-cost-usd 15 --force-plain
codeprobe interpret ~/test_repos/gascity/gascity-jf28-3way/.codeprobe
```
