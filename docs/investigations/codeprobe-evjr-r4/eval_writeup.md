# codeprobe-evjr.4 — Narrow MCP tool surface forces local Read (SDLC three-arm A/B)

**Status:** complete
**Bead:** `codeprobe-evjr.4` (workflow `codeprobe-k3ruz`, formula `mol-focus-review`)
**Type:** experiment-config-only change + N=3 three-arm eval
**Model:** claude-sonnet-4-6 · **max_turns:** 50 (all arms) · **total cost:** $118.40

## TL;DR

Blocking the Sourcegraph **read/browse** MCP tools (`read_file`, `list_files`,
`list_repos`, `commit_search`, `diff_search`, `compare_revisions`) via
`disallowed_tools` does more than shift read traffic to local `Read` — it makes
the agent **abandon Sourcegraph entirely** and revert to a fully-local
Bash/Read/Edit workflow nearly identical to `baseline`. On the 5-task gascity
SDLC suite this **cut output tokens 53.5 %** and **cost 21 %** versus full
`with-sourcegraph`, while reward **did not regress** (it rose, within the noise
of a small high-variance sample). The bead's predicted outcome holds; the
falsifying outcome (agent compensates with `keyword_search`, cost stays flat) is
**rejected** — `keyword_search` usage in the narrow arm was zero.

## Setup

| | |
|---|---|
| Experiment | `gascity-mcp-comparison` (`.codeprobe/experiment.json`) |
| Tasks | mcn7's 5 SDLC tasks: `0d4ec3ad, 45b581b5, ba1f3675, d906ac3d, fde8e6e0` (suite `suite-sdlc.toml`) |
| Arms | `baseline` (no MCP) · `with-sourcegraph` (full SG) · `with-sg-narrow` (SG minus read/browse) |
| Repeats | N=3 → 45 trials |
| Run | `codeprobe run … --suite suite-sdlc.toml --repeats 3 --parallel 3 --config-parallel 1 --max-cost-usd 140` |

**`with-sg-narrow` is `with-sourcegraph` + one delta:** a `disallowed_tools`
list. All other fields (preamble `sourcegraph`, `mcp_config`, `max_turns=50`,
built-in tool surface) are identical, so the comparison isolates the tool block.

### Implementation note — corrected the bead's tool names + mechanism

- **Tool names:** the bead's example used `sg_`-prefixed identifiers
  (`mcp__sourcegraph__sg_read_file`). The **actual** MCP tool identifiers have
  no `sg_` prefix (`mcp__sourcegraph__read_file`), verified from
  `runs.codeprobe-mcn7/trace.db`. Using the bead's names verbatim would have
  matched nothing and silently invalidated the experiment.
- **Mechanism:** the bead specified an `allowed_tools` whitelist. The claude
  adapter treats `--allowedTools` as *auto-approve only*; `--disallowedTools`
  *blocks outright* (`adapters/claude.py` comment + `build_command` verified to
  emit `--disallowedTools mcp__sourcegraph__read_file,…`). `disallowed_tools` is
  therefore the reliable way to *force* the drop and also the smaller delta (the
  built-in surface stays identical to `with-sourcegraph`).
- **No reuse of mcn7 baseline/with-sg data:** stale since mcn7 (`sg_repo` now
  populated; preamble gained the "Workspace Source Priority" guard). All three
  arms ran fresh under identical current conditions.

## Results

```
config              n   reward    cost$  cost/t   time_s   out_tok localRead%
baseline           15    0.197    34.81    2.32      565     22251       100%
with-sourcegraph   15    0.075    46.70    3.11     1027     53049         1%
with-sg-narrow     15    0.323    36.88    2.46      622     24692       100%
```

### Acceptance criteria

1. **output_tokens drop ≥ 30 % vs with-sourcegraph** — 24 692 vs 53 049 =
   **−53.5 %.** ✅ PASS.
2. **with-sg-narrow local Read ≥ 30 % of read traffic** — **100 %**
   (285 local `Read`, 0 `mcp__sourcegraph__read_file`). ✅ PASS.
3. **reward holds within ±0.05 of baseline** — narrow 0.323 vs baseline 0.197 =
   **+0.126.** Strictly outside ±0.05, but in the **favourable** direction (no
   regression). The honest reading is "reward does not degrade," not "narrow
   beats baseline" — see caveats.

**Predicted outcome:** confirmed (output tokens −53.5 %; reward held).
**Falsifying outcome (keyword_search compensation, flat cost):** rejected —
`keyword_search` usage in narrow = 0; cost fell 21 %.

### Per-config tool histograms (the actual story)

```
[baseline]            Bash 527, Read 303, Edit 46, Agent 3
[with-sourcegraph]    mcp_read_file 544, keyword_search 235, diff_search 63,
                      Write 37, commit_search 34, list_files 29, Read 5,
                      nls_search 5, evaluator 4, list_repos 1   (Bash 0, Edit 0)
[with-sg-narrow]      Bash 477, Read 285, Edit 54               (0 MCP calls)
```

Two things jump out:

- **`with-sourcegraph` used zero `Bash` and zero `Edit`.** It spent its 50-turn
  budget on 544 `read_file` calls and only `Write` (37) for edits — it largely
  failed to make and verify real code changes, which is why its reward collapsed
  to 0.075 (well *below* baseline). This matches the evjr audit: MCP makes the
  agent loop on `read_file` on this SDLC family.
- **`with-sg-narrow` used zero MCP tools of any kind** — not just the blocked
  ones. Its histogram is a near-clone of `baseline`. Blocking `read_file` didn't
  merely redirect reads; it collapsed the whole MCP-loop attractor and returned
  the agent to the local edit/test workflow that actually scores.

### Why zero SG search calls is *correct* here (not a broken MCP)

The task workspace **is** the `sg_repo` (`github.com/gastownhall/gascity`), so
every file the agent needs is on local disk. The `sourcegraph` preamble's
"Workspace Source Priority (READ FIRST)" guard says: use local `Read`/`Grep`/
`Glob` for workspace files, use SG only for cross-repo. With no cross-repo need,
zero SG usage is the intended behaviour. The verbal guard is **ignored** in
`with-sourcegraph` (544 `read_file` calls on local files); blocking `read_file`
makes the guard actually bind.

**MCP-connection confound ruled out:** `with-sg-narrow` uses an identical
`mcp_config` (same URL/token, `--strict-mcp-config`) to `with-sourcegraph`,
which made 900+ successful SG calls. Both arms show the same `mcp_servers:
[{sourcegraph, status: pending}]` init snapshot (a pre-handshake snapshot, not a
failure). The SG *search* tools were available in narrow; the agent chose not to
use them.

## Caveats (honesty)

- **Small, high-variance sample.** N=3×5 with many 0.0 trials (failed
  test-script verification). The reward gap narrow−baseline (+0.126) is within
  plausible noise; do **not** claim narrow *improves* over baseline. The
  defensible claim: narrowing **recovers baseline-class behaviour and reward**
  while removing the MCP cost penalty.
- **Family-specific.** This is the gascity SDLC family, where workspace == sg_repo.
  On genuine cross-repo tasks (oracle_overlap, org-scale) SG search retains value;
  a blanket `read_file` block there is not implied by this result.
- **Reward scoring.** Continuous scorer (`scorer_family=continuous`,
  weight_direct/artifact 0.5/0.5); `with-sourcegraph`'s 0.075 reflects test
  failures from incomplete edits, not a scoring artefact.

## Recommendation

For SDLC / single-repo configs where the workspace is the indexed repo, blocking
the SG read/browse tools (or, equivalently, enforcing the preamble's
workspace-priority rule) removes the MCP cost penalty at no reward cost. This is
a stronger, more reliable lever than the r2 verbal preamble nudge alone. Pair
with the `max_turns` cap (already in place) for cost containment.

## Reproduce

```bash
source /home/ds/projects/codeprobe/.env.local
codeprobe run /home/ds/test_repos/gascity/gascity-mcp-comparison \
  --suite docs/investigations/codeprobe-evjr-r4/suite-sdlc.toml \
  --repeats 3 --parallel 3 --config-parallel 1 --max-cost-usd 140 --force-plain
python3 docs/investigations/codeprobe-evjr-r4/analyze.py
```

Artifacts: `analyze.py`, `analyze.out`, `per_config_summary.json`, `suite-sdlc.toml`,
`logs/`. Raw run: `.codeprobe/runs/{baseline,with-sourcegraph,with-sg-narrow}/` +
shared `trace.db`.
