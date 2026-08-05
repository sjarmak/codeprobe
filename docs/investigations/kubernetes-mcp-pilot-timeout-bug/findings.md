# kubernetes MCP pilot — `--timeout` doesn't reach mined `time_limit_sec`

**Setup:** 5 mined `org_scale_cross_repo` tasks (`--goal mcp`, difficulty
`hard`, 7–2207 files each) on `~/test_repos/kubernetes`, comparing
`baseline` (agent claude, no MCP) vs `with-sourcegraph-mcp` (Sourcegraph
MCP over `https://demo.sourcegraph.com/.api/mcp/all`). Images built
locally from `src/codeprobe/sandbox/Dockerfile.agent` and
`src/codeprobe/sandbox/Dockerfile.scoring`, then
bootstrapped via a throwaway `localhost:5000` registry (no production
registry available in this environment). Run dir:
`~/test_repos/kubernetes/.codeprobe/runs/`.

Three run attempts, **$31.41 total spend**, `codeprobe interpret`
correctly refused a verdict every time (`VALIDITY_FAILED` / "NOT
COMPARABLE — below the 3-task paired-comparison floor"). The pilot's
original goal — a quotable baseline-vs-MCP comparison — was not
reached. What it did surface is a reproducible bug in `codeprobe run`.

## Headline: the actual bug

Every mined task's `metadata.json` carries `time_limit_sec: 300`
(the `codeprobe.models.task` default). `codeprobe run --timeout <N>`
does **not** override it, contrary to its help text ("Timeout in
seconds per task"). Evidence, across three independent runs:

| Run | `--timeout` passed | Failures land at | Successes finish in |
|---|---|---|---|
| 1 | *(not set → resolves to 3600s default)* | 300.2–300.6s | up to ~270s |
| 2 | `1200` | ~296–301s | n/a (baseline partial, MCP arm 0/5) |
| 3 | `1200` | 300.3–301.7s | 126–300s |

Across totally different task subsets failing in each run, every
single failure clusters in a ~1.5s band around exactly 300s —
regardless of whether `--timeout` was unset (implying a 3600s
resolved value per `run_cmd.py:1273`) or explicitly set to 1200s.
Successful tasks never approach 300s. This is not organic task
difficulty variance; it's a hard, unoverridden ceiling that traces to
the per-task mined `time_limit_sec`, not to `AgentConfig.timeout_seconds`
(`resolved_timeout`, which the CLI flag *does* reach per source
inspection — the override plumbing exists but something downstream of
it isn't consulting it for the actual kill switch).

**Impact:** any mined "hard" org-scale task (hundreds to thousands of
files) is effectively uncapped-in-theory but 300s-capped-in-practice.
`--timeout` gives operators false confidence that raising it fixes
attrition on large tasks — it doesn't.

## Implementation update (2026-08-04)

The kill switch was traced to `_resolve_task_timeout_seconds()` in
`src/codeprobe/core/executor.py`, which always
selected `min(AgentConfig.timeout_seconds, metadata.time_limit_sec)` and had
no way to distinguish an explicit CLI value from a resolved default.
`codeprobe-isun.7.3.8` fixes the precedence while retaining the metadata
safety cap by default:

1. An explicit `codeprobe run --timeout N` now reaches the adapter as the
   effective per-task timeout and outranks `metadata.time_limit_sec`.
2. With no explicit flag, a valid task metadata limit still caps the resolved
   experiment/default timeout.
3. Auto-resolved `strict` and `pragmatic` policies for the generated
   `sourcegraph` MCP server block `mcp__sourcegraph__evaluator`. Explicit user
   tool policies remain authoritative.

The global `org_scale_cross_repo` mining default was not raised. Once the
explicit override works, selecting a larger family-wide default would be a
separate duration-policy decision rather than part of this confirmed bug fix.

## Secondary finding (worked around, not a bug): `evaluator` is unstable non-interactively

Run 2 used `mcp_mode=pragmatic` (blocks Bash/Grep/Glob, keeps
Read/Write + MCP). With its usual exploration tools gone, the agent
reached for `mcp__sourcegraph__evaluator` — the Sourcegraph MCP
server's arbitrary search-script execution tool — and issued
unbounded queries (observed: a Lua script matching every file
containing the letter "e", org-wide). All 5 MCP-arm runs in that
round died with `terminal_reason: "aborted_streaming"` after an
interrupted `evaluator` tool call, clustered within seconds of each
other (~20:10:27Z) regardless of task progress — consistent with the
demo Sourcegraph instance's own request timeout firing on an
expensive query and aborting the Claude Code stream.

Fix applied for run 3: `codeprobe experiment update-config . --label
with-sourcegraph-mcp --disallowed-tools mcp__sourcegraph__evaluator`.
That resolved it cleanly — no more stream aborts, no more `evaluator`
calls in the telemetry. Worth considering whether `evaluator` should
default-excluded from `pragmatic`/`strict` tool-surface policies, or
at minimum documented as unsuited to non-interactive/autonomous runs
given it has no visible cost/scope guardrail.

## What data survived (below the paired-comparison floor — reference only, not a verdict)

| Run | Config | task | status | score (F1) | verdict | duration | tools used |
|---|---|---|---|---|---|---|---|
| 1 | baseline | 826dfd3a (2207 files) | completed | 0.407 | incorrect | — | Bash only |
| 1 | baseline | 8faa5715 (7 files) | completed | 0.000 | incorrect | — | Bash only |
| 1 | baseline | d8963b9b (151 files) | completed | 0.340 | incorrect | — | Bash only |
| 1 | baseline | 16e29353 (1007 files) | **error** | — | — | timeout | — |
| 1 | baseline | 3b711ac4 (683 files) | **error** | — | — | timeout | — |
| 1 | mcp | 16e29353 (1007 files) | completed | 0.613 | **correct** | — | keyword_search×3, list_repos, evaluator×2 |
| 1 | mcp | d8963b9b (151 files) | completed | 0.309 | incorrect | — | Bash only (MCP available, unused) |
| 1 | mcp | 826dfd3a, 3b711ac4, 8faa5715 | **error** ×3 | — | — | timeout | — |
| 2 | baseline | 4/5 completed, 1 timeout | — | mean 0.376 | — | — | (per-task detail not captured) |
| 2 | mcp | **0/5 completed, 5/5 aborted_streaming** | — | — | — | evaluator loop | see above |
| 3 | baseline | 8faa5715 (7 files) | completed | 0.000 | incorrect | 126s | Bash×13 |
| 3 | baseline | 16e29353 (1007 files) | completed | 0.901 | **correct** | 159s | Bash×11 |
| 3 | baseline | 826dfd3a (2207 files) | completed | 0.662 | **correct** | 227s | Bash×22 |
| 3 | baseline | 3b711ac4, d8963b9b | **error** ×2 | — | — | timeout (300.3–300.4s) | — |
| 3 | mcp | 16e29353 (1007 files) | completed | 0.835 | **correct** | 164s | keyword_search, list_repos |
| 3 | mcp | 3b711ac4, 826dfd3a, 8faa5715, d8963b9b | **error** ×4 | — | — | timeout (300.4–301.7s) | — |

The one task both arms ever completed *in the same run* is
`16e29353` in run 3: baseline F1 0.901 (correct) vs. MCP F1 0.835
(correct) — baseline slightly ahead, despite the MCP arm genuinely
invoking Sourcegraph tools. No run produced evidence of an MCP
advantage on tasks that completed in both arms; the sample is just
too thin (n=1) to say anything with confidence either way.

## Answers vs. still open

Answers:

- **The image bootstrap / mine / run / interpret pipeline works
  end-to-end**, including a from-scratch local-registry bootstrap
  path (no production registry needed) and `interpret`'s validity
  gate correctly refusing to quote underpowered runs.
- **`--timeout` does not reach the mined-task time limit.** Confirmed
  reproducibly across 3 runs, 2 different `--timeout` values (unset →
  3600s default, and explicit 1200s), all clustering at ~300s failure.
- **`mcp__sourcegraph__evaluator` is not safe to leave enabled in
  non-interactive eval runs** without a query-cost guardrail; blocking
  it via `--disallowed-tools` is a clean, working mitigation.

Doesn't answer:

- Whether Claude Code + Sourcegraph MCP actually helps on org-scale
  kubernetes navigation tasks — needs a real run once the timeout bug
  is fixed or worked around.
- At pilot time, where exactly the 300s cap was enforced. The implementation
  update above now answers this: `_resolve_task_timeout_seconds()` applied the
  mined metadata cap after CLI config resolution.

## Original followups and disposition

- **Resolved:** fix and document the `--timeout` vs. mined `time_limit_sec`
  precedence gap.
- **Not changed:** consider whether `codeprobe mine --goal mcp` should set a larger
  `time_limit_sec` by default for `org_scale_cross_repo` tasks
  spanning hundreds-to-thousands of files. This remains a separate policy
  decision; the confirmed precedence bug did not require it.
- **Still pending:** rerun the same 5 tasks with `mcp_mode=pragmatic` and
  `--pristine-config`, and raise
  `--max-cost-usd` to ~$25. Estimated cost similar to run 3 (~$10)
  since the per-task failures should convert to completions rather
  than add new work. The automatic Sourcegraph policy now blocks `evaluator`,
  so a new config does not need the explicit tool workaround.

## Reproducer

```bash
# Mine (already done for this repo; suite at .codeprobe/suite.toml)
codeprobe mine ~/test_repos/kubernetes --json --no-interactive --goal mcp --count 5

# Configs already registered in ~/test_repos/kubernetes/.codeprobe/experiment.json:
#   baseline              — agent claude, no MCP
#   with-sourcegraph-mcp  — mcp_mode=pragmatic, instruction_mcp.md,
#                            disallowed_tools=[mcp__sourcegraph__evaluator]

codeprobe run ~/test_repos/kubernetes \
  --agent claude --suite ~/test_repos/kubernetes/.codeprobe/suite.toml \
  --timeout 1200 --pristine-config --max-cost-usd 25 --parallel 5 --json
```

Run dir: `~/test_repos/kubernetes/.codeprobe/runs/`.
