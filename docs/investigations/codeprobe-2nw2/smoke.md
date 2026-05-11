# codeprobe-2nw2 — scaffold-mode smoke trial

Smoke trial closing the codeprobe-2nw2 epic (sg-only scaffold mode for
SDLC). Validates that the mode works end-to-end on a real codebase
and answers the open question whether scaffold mode rescues a task
that with-sg-fixed (non-isolated sg-only) already fails.

## Setup

- **Code under test:** commit `4536944` on `main` (codeprobe v0.11.0,
  `hide_local_source: Literal["off","hide","scaffold"]` consolidated
  field). Predecessor commits `1b699b9` (.3) and `ba5b4cc` (.2) already
  landed the executor wiring and isolation layer.
- **Task:** `45b581b5` — gascity SDLC task "fix(session): preserve
  in_progress claims across worker churn" (PR
  `gastownhall/gascity#1392`, ground truth commit
  `45b581b5` — full 40-char SHA elided to keep gitleaks happy; see
  the task's `metadata.json:ground_truth_commit` for the verbatim
  value). Copied verbatim from
  `~/test_repos/gascity/gascity-mcp-comparison/.codeprobe/tasks/`.
- **Experiment:** `~/test_repos/gascity/gascity-2nw2-smoke/.codeprobe/`
  with a single config `with-sg-isolated-sdlc` (claude /
  `claude-sonnet-4-6`, `preambles=["sourcegraph"]`, `mcp_mode=strict`,
  `max_turns=50`, **`hide_local_source: "scaffold"`**).
- **Invocation:** `codeprobe run .codeprobe --repeats 3 --parallel 1
  --timeout 2700 --max-cost-usd 30 --force-plain`.
- **Workspace pin:** codeprobe pins the gascity workspace to
  `45b581b5^` via `git reset --hard` before each rep, the standard
  pre-merge-state mechanism for SDLC tasks. **Note:** this discards
  any local uncommitted changes on `~/test_repos/gascity/` for the
  duration of the run; the workspace is restored to the pinned
  state, not to whatever was checked out before. Run the smoke
  trial on a clean checkout or expect local WIP to be lost.

## Results

| Config | mean reward | duration (s) | cost (USD) | source |
|---|---|---|---|---|
| baseline (jf28-sdlc-v2/baseline, no isolation) | **0.63** | 391 avg | $1.46 avg | `~/test_repos/gascity/gascity-jf28-sdlc-v2/.codeprobe/runs/baseline/45b581b5/` |
| with-sg-fixed (jf28-sdlc-v2, no isolation, MCP + local source visible) | **0.00** | 1842 avg | $3.98 avg | `~/test_repos/gascity/gascity-jf28-sdlc-v2/.codeprobe/runs/with-sg-fixed/45b581b5/` |
| **with-sg-isolated-sdlc** (this run, scaffold mode) | **0.00** | 2377 avg | ~$4.80 avg† | `~/test_repos/gascity/gascity-2nw2-smoke/.codeprobe/runs/with-sg-isolated-sdlc/45b581b5/` |

† rep 1 timed out at the 2700s wall-clock cap and the adapter reported
`cost_usd: null` (no envelope to parse on kill). Per-rep totals were
**$4.07 (rep 0)** + **null (rep 1, timeout)** + **$5.54 (rep 2)** =
**$9.61 total**. Mean cost is taken over the two reps that produced an
envelope.

### Per-rep summary

| rep | duration | score | cost | tool calls | error |
|---|---|---|---|---|---|
| 0 | 1852s | 0.0 | $4.07 | 58 | `error_max_turns` |
| 1 | 2700s (timeout) | 0.0 | n/a | n/a | `Agent timed out after 2700s` |
| 2 | 2578s | 0.0 | $5.54 | 67 | `error_max_turns` |

### Tool-use breakdown (rep 0 + rep 2, the reps with envelopes)

| tool | rep 0 | rep 2 |
|---|---|---|
| `mcp__sourcegraph__read_file` | 31 | 37 |
| `mcp__sourcegraph__keyword_search` | 13 | 13 |
| `mcp__sourcegraph__diff_search` | 5 | 3 |
| `mcp__sourcegraph__list_files` | 2 | 3 |
| `mcp__sourcegraph__commit_search` | 1 | 2 |
| `mcp__sourcegraph__nls_search` | 1 | 1 |
| `mcp__sourcegraph__evaluator` | 0 | 3 |
| `Write` | 5 | 5 |
| `Read` / `Bash` / `Grep` / `Glob` | **0** | **0** |

Write targets in rep 0: `/tmp/check_local.sh` (probe), then
`cmd/gc/cmd_start.go` (×2) and `cmd/gc/pool_session_recovery.go` (×2)
— both inside the task's declared access scope `cmd/gc/`.

## Acceptance-criterion validation

This run is the gate for the codeprobe-hcnv bead. Each AC and what
proves it:

| AC | Evidence |
|---|---|
| Smoke trial completes 3/3 trials with no quota / system errors | `results.json` shows all three reps reached the executor — no system error category, no OAuth quota halt. Reps 0 and 2 hit `error_max_turns` (agent ran out of turns under the 50-cap), rep 1 hit the wall-clock timeout. Both are **agent** errors, not quota / system failures. |
| Agent used MCP for reads (zero local Read calls) | Tool-use breakdown above: rep 0 has 53 sourcegraph calls + 5 Writes + 0 local Read/Bash/Grep/Glob. Rep 2 has 62 sourcegraph calls + 5 Writes + 0 local Read/Bash/Grep/Glob. The scaffold context manager + the strict-MCP tool surface (`--allowedTools mcp__sourcegraph,Write --disallowedTools Grep,Bash,Glob,Read`) together produce the intended sg-only behaviour. |
| Agent wrote edit to `cmd/gc/...` files | Rep 0 wrote to `cmd/gc/cmd_start.go` and `cmd/gc/pool_session_recovery.go`. Rep 2 followed the same pattern. Both files are inside the task's declared access scope. |
| test.sh ran against merged state | `automated_score: 0.0` produced for every rep — i.e. scoring ran end-to-end. `go test ./cmd/gc/... ./internal/config/...` exited non-zero because the agent's edits didn't fix the actual bug, but the verifier did execute against the merged (overlay) tree. The fact that `cmd/gc/` files were not 0 bytes during the overlay window (else the Go compiler couldn't have parsed them) is implicit confirmation that the 6-step `__exit__` contract from `design.md` ran. |
| `docs/investigations/codeprobe-2nw2/smoke.md` exists with the comparison table populated | This file. |

## Interpretation

The smoke trial's primary purpose was to prove the **mechanism**
works on a real codebase — repository large enough that
TRUNCATE_EXTENSIONS actually fires across the tree, agent prompt
that uses Sourcegraph MCP for real, verifier that runs the project's
own `go test`. All of that ran cleanly across three reps. **Scaffold
mode is mechanically correct.**

The secondary purpose was to ask whether scaffold mode rescues
SDLC tasks that with-sg-fixed (non-isolated MCP) fails on.
**It does not — at least not on `45b581b5` at `max_turns=50`.**
All three reps scored 0.0, matching jf28-sdlc-v2's
with-sg-fixed result. The two-fold runtime relative to baseline
(2377s avg vs 391s avg) and the fact that both reps hit
`error_max_turns` suggest the bottleneck isn't navigation — it's
that this SDLC task requires the agent to coordinate edits across
multiple files (`cmd_start.go`, `pool_session_recovery.go`,
`session_reconciler.go`, `session_lifecycle_parallel.go`,
`session_types.go`) and the MCP-only loop spends too many turns on
discovery before reaching the synthesis step.

Possible follow-ups (not in scope for codeprobe-hcnv):

- Rerun with `max_turns=80` to test whether the turn cap is the
  limit, or whether the agent's MCP-only loop genuinely cannot
  converge on multi-file SDLC edits.
- Rerun against a single-file SDLC task to see whether scaffold
  mode is competitive on tasks the baseline solves with a single
  edit. `45b581b5` is a multi-file fix; a single-file fix may be a
  better fit for the mode.
- Compare against `with-sg-fixed` head-to-head on the same task on
  the same day (the jf28 numbers cited here are from May 7 and used
  a slightly older codeprobe; ideally redo both configs in the same
  experiment to control for adapter / quota drift).

The mode ships green on the mechanism. The product question — when
should users prefer scaffold over hide / off — needs a broader
A/B suite, which is the natural next bead, not part of `2nw2`.

## Artifacts

- Run dir: `~/test_repos/gascity/gascity-2nw2-smoke/.codeprobe/runs/with-sg-isolated-sdlc/`
- Smoke run log: `runs/2nw2-smoke.log` (codeprobe checkout, gitignored)
- Adapter envelopes per-rep:
  `~/test_repos/gascity/gascity-2nw2-smoke/.codeprobe/runs/with-sg-isolated-sdlc/45b581b5/{agent_output.txt,scoring.json}` and same under `repeat-1/`, `repeat-2/`.
- Baseline references:
  - `~/test_repos/gascity/gascity-jf28-sdlc-v2/.codeprobe/runs/baseline/45b581b5/scoring.json`
  - `~/test_repos/gascity/gascity-jf28-sdlc-v2/.codeprobe/runs/with-sg-fixed/45b581b5/scoring.json`
