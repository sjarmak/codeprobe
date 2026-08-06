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

**Runs 1–3** (below), totaling $31.41, all failed `codeprobe interpret`'s
validity gate (`VALIDITY_FAILED` / "NOT COMPARABLE — below the 3-task
paired-comparison floor") and surfaced a reproducible bug in
`codeprobe run`. **Runs 4–5**, after the fix landed as `9e38d21`, cleared
the gate mechanically. Grand total across all 5 runs: **$69.48**.

> **Read this first.** The timeout bug and the two oracle defects in
> "Confound audit" are the durable findings. Run 5's baseline-vs-MCP
> *ranking* is retracted: the arms differed on three axes at once and the
> oracle's reference implementation is the baseline arm's own tool, so
> the result says nothing about the MCP. See
> [Confound audit](#confound-audit-2026-08-05-retract-run-5s-ranking).

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

## Post-fix validation (2026-08-04, runs 4–5): the timeout fix holds

Upgraded the local install to `9e38d21` (`uv tool upgrade codeprobe`) and
reran. **Run 4** (`--timeout 1200`, `--pristine-config`,
`--max-cost-usd 25`) came back with `infra_failure_count: 0` on both
configs for the first time — the timeout fix holds.

Run 4's `with-sourcegraph-mcp` config still carried the manual
`--disallowed-tools mcp__sourcegraph__evaluator` override from run 3.
Per `mcp_policy.py`'s documented precedence ("explicit `disallowed_tools`
on an `ExperimentConfig` always wins... auto-restriction only runs when
neither field is set"), that override silently downgraded the resolved
policy from `pragmatic` to `explicit` — `mcp_mode: "pragmatic"` stayed in
the config file, but the Grep/Bash/Glob auto-block never applied. Telemetry
confirmed it: 2 of 5 MCP-arm tasks in run 4 made **zero** MCP tool calls
and used Bash 10–21 times instead. Run 4's `interpret` output ("baseline
nominally ahead, p=0.125") is discarded — not a clean ablation, just a
lucky mid-state between `loose` and `pragmatic`.

Fix: cleared the config's `disallowed_tools` back to `null` (the
`9e38d21` fix already auto-blocks `evaluator` inside `pragmatic`/`strict`,
so the manual override was never needed post-fix — it's actively harmful
to set it, since any explicit `allowed_tools`/`disallowed_tools` opts a
config out of the mode's auto-policy entirely). Reran as **run 5**.
`infra_failure_count: 0` again, and this time the MCP arm genuinely ran
MCP-only (zero Bash/Grep/Glob calls across all 5 tasks, confirmed in
`tool_use_by_name`).

**Run 5 result** (`codeprobe interpret`, `validity.passed: true`, 10/10 trials scored):

| Config | mean F1 | pass_rate | mean cost/task | mean duration | total cost |
|---|---|---|---|---|---|
| baseline | 0.458 | 0.4 (2/5 correct) | $0.89 | 198s | $4.47 |
| with-sourcegraph-mcp | 0.319 | 0.2 (1/5 correct) | $4.69 | 511s | $23.46* |

*Total run cost $27.93 — the `--max-cost-usd 25` cap tripped mid-run;
the 5 already-dispatched parallel MCP-arm tasks were allowed to finish
rather than hard-killed. Worth knowing going in: budget the cap for
worst-case in-flight overshoot on high-parallelism runs, not the nominal
ceiling.

`interpret`'s pairwise comparison: **baseline nominally ahead, +14%
score, $18.98 cheaper, 313s faster per task on average — not
significant at p=0.05 (p=0.625, n=5, Cohen's d=0.48)**.

> **Retracted as a claim about the MCP.** The per-task numbers below are
> reproducible and left intact, but the arms are confounded three ways
> and the metric's reference implementation is grep. Read
> [Confound audit](#confound-audit-2026-08-05-retract-run-5s-ranking)
> before quoting anything in this section. In particular `8faa5715`'s
> answer key is invalid, so the row below is noise in both arms.

| task | files | baseline F1 (verdict) | mcp F1 (verdict) | mcp cost vs baseline | mcp MCP-tool calls |
|---|---|---|---|---|---|
| 16e29353 | 1007 | 0.930 (correct) | 0.261 (incorrect) | 5.5× | keyword_search×38, list_repos×2, list_files×4 |
| 8faa5715 | 7 | 0.000 (incorrect) | 0.000 (—) | 9.1× | keyword_search×51, commit_search×1, list_files×9 |
| 826dfd3a | 2207 | 0.387 (incorrect) | 0.323 (incorrect) | 5.2× | keyword_search×20, read_file×4, list_repos×1 |
| d8963b9b | 151 | 0.332 (incorrect) | **0.484 (incorrect)** | 6.3× | keyword_search×56, list_files×26, list_repos×1 |
| 3b711ac4 | 683 | 0.641 (correct) | 0.530 (correct) | 2.9× | keyword_search×39, read_file×4, list_files×4 |

MCP wins on raw score in exactly 1 of 5 tasks (`d8963b9b`, and only
marginally — still "incorrect"), loses or ties on the rest, and costs
2.9×–9.1× more everywhere. The dominant MCP-arm pattern is very high
`keyword_search` call counts (20–56 per task) — under `pragmatic`
isolation, the agent falls back to many narrow keyword searches to
reconstruct a file list that a single `grep -r` (baseline's actual
behavior, per `tool_use_by_name: {'Bash': N}`) gets in one shot. That
interaction cost, not model reasoning quality, looks like the primary
driver of both the cost gap and (via truncated exploration under the
1200s timeout) some of the score gap.

Caveat this still deserves: n=5 tasks, single repeat, one demo
Sourcegraph instance, one task family (`org_scale_cross_repo` /
file-discovery-by-marker). Not a claim about MCP tooling in general —
just this task shape, this isolation mode, this instance.

## Confound audit (2026-08-05): retract run 5's ranking

Run 5 was mechanically valid (`validity.passed: true`, 10/10 scored) and
I reported "baseline nominally ahead". A follow-up audit shows that
ranking is **not attributable to the MCP**, and that the pilot as
designed cannot answer the question it was built to answer. The numbers
below are reproducible; the arm labels are what mislead.

### A one-line grep beats both agents

The org-scale oracle is `oracle_type: "file_list"` whose `expected` set
is exactly `{tracked files matching family.content_patterns}`. So the
task's reference implementation is a single `git grep -lE`. Scoring that
one-liner against the shipped answer keys:

| task | family | `git grep` F1 | baseline F1 | mcp F1 |
|---|---|---|---|---|
| d8963b9b | migration-inventory | 0.997 | 0.332 | 0.484 |
| 3b711ac4 | compliance-audit | 0.974 | 0.641 | 0.530 |
| 826dfd3a | platform-knowledge | 1.000 | 0.387 | 0.323 |
| 16e29353 | incident-debug | 1.000 | 0.930 | 0.261 |
| **mean (4 valid)** | | **0.992** | **0.572** | **0.399** |

Both agents lose to `grep` by a wide margin. The metric scores regex
reconstruction, not repository understanding, and it has no room for
judgment: baseline's `16e29353` transcript explicitly excludes `vendor/`
(729 files) and bare `fmt.Errorf` propagation, reasoning that including
the latter "would have made the answer approximate every Go file in the
repo." Defensible engineering, penalized by the key.

This inverts the pilot's premise. A semantic code-intelligence tool is
being graded on mechanical pattern reproduction, and the arm holding the
pattern-reproduction tool (`Bash`/grep) was the one that placed second.

### Attribution: the oracle dominates, and it is entangled with tool surface

**1. Evaluator setup — dominant.** The oracle's generating function is
grep. Baseline had `Bash`. The MCP arm ran under `pragmatic`, which
blocks `Grep`/`Bash`/`Glob`. The arms therefore differ on *access to the
oracle's own implementation*, which is not a property of the MCP.

Recall accounts for roughly 90% of the gap; precision is close to even:

| arm | mean precision | mean recall | mean answer-set size |
|---|---|---|---|
| baseline | 0.506 | **0.830** | 1207 files |
| with-sourcegraph-mcp | 0.463 | **0.451** | 675 files |

The precision gap is 0.043 against a recall gap of 0.379, and MCP
precision is higher on 2 of the 4 tasks. So the MCP arm returns about
half as many files at a comparable hit rate: correct but incomplete.
That is what a ranked, paginated search API does when pressed into
exhaustive enumeration. The arm issued 20–56 `keyword_search` calls per
task trying to approximate one `grep -rl` and still reached only ~45%
recall. The mismatch is in the API shape, not the model's reasoning.

**2. Tool surface — major, inseparable from #1.** `pragmatic` makes this
a *substitution* test (MCP instead of local search). Production Claude
Code + Sourcegraph is *augmentation*. I selected `pragmatic` in run 3 to
stop the arm degenerating to baseline, but on a grep-shaped task
"degenerate to grep" is the correct behavior, so the config engineered
away the right answer. It was not even clean substitution: `Read` stays
enabled under `pragmatic` and the MCP arm used it 6–34 times per task.

**3. Preamble — minor, misaligned, unquantifiable here.**
`instruction_mcp.md` appends 25 lines to the MCP arm only, so prompt
length is an uncontrolled difference. Its content is also wrong for
these tasks: it says "use them to ground your **edits**" when the tasks
are read-only enumeration, and it advertises `symbol_references` and
`go_to_definition`, which the agent invoked **zero times across all five
tasks** in run 5. It omits `list_files` and `nls_search`, which the agent
actually used. The advertised names also don't match the bound tool
names (`find_references` vs `symbol_references`), which the preamble
acknowledges by telling the agent not to assume a naming scheme. Real
effect, but no arm isolates it, so it cannot be sized from this data.

### Two codeprobe defects found while auditing

**(a) `8faa5715` ships a 0%-valid answer key.** Its instruction asks for
"the complete set of all files matching any deprecation marker pattern
throughout the entire repository." Its `expected` list holds 7 files, of
which **zero** match the `migration-inventory` patterns, while omitting
all 152 files that do. One entry is `vendor/golang.org/x/crypto/...`,
which `_filter_by_suffix` explicitly excludes from scanning — so the key
cannot have come from the family scan that generated the instruction.
Instruction generation and oracle generation diverged for this task. It
is a sibling of `d8963b9b` (same family, 151 files, 100% valid) with
**zero overlap** between the two keys. Both arms correctly scored 0.0;
the task contributed nothing but noise and depressed both means.

Validation of all five shipped keys against a faithful re-implementation
of `_scan_files`:

| task | family | key size | true matches | key validity | true positives omitted |
|---|---|---|---|---|---|
| d8963b9b | migration-inventory | 151 | 152 | 100.0% | 1 |
| **8faa5715** | migration-inventory | **7** | **152** | **0.0%** | **152** |
| 3b711ac4 | compliance-audit | 683 | 652 | 95.2% | 2 |
| 826dfd3a | platform-knowledge | 2207 | 2209 | 100.0% | 2 |
| 16e29353 | incident-debug | 1007 | 1008 | 100.0% | 1 |

Four of five keys are sound and near-exhaustive, so this is an isolated
generation bug rather than a systemic oracle problem. Worth a
mine-time assertion that `expected` is non-empty *and* intersects the
family scan before a task ships.

> **Root cause found (2026-08-05).** `8faa5715` is the multi-hop (`-mh`)
> variant of the migration-inventory family, so its key is *callers of*
> deprecated symbols, not files carrying the markers — a set that
> legitimately does not intersect the family scan (which is why the
> intersection assertion proposed above would reject valid tasks and was
> not the fix that landed). Three defects combined:
>
> 1. `find_callers_of_symbols` did `break`, not `continue`, when it reached
>    a symbol-defining file while iterating an unordered set, so the caller
>    scan aborted early — 7 files instead of the full set.
> 2. That scan skipped the `vendor/`/`node_modules/`/`testdata/` exclusion
>    the single-hop scan applies, which is how a `vendor/golang.org/...`
>    path entered a key the family scan structurally cannot emit.
> 3. `_build_task_gen_prompt` told the question-writing model that the
>    answer key was the *single-hop* pattern-match set (with its count),
>    then demanded the question describe "the SAME set" — so the model
>    wrote a marker-enumeration question for a caller-set key.
>
> All three are fixed. The gate that would have caught this regardless is a
> mine-time model check on question/key agreement, wired into org-scale
> mining (`validate_ground_truth_sample` existed for exactly this and was
> called from nothing but tests); a task whose sampled key is majority-
> rejected as an answer to its own question is now dropped at mine time.

**(b) Ground-truth / workspace commit skew.** Keys were built at
`c6a95ffd`; every trial logged `Pinned workspace to c6a95ffd^
(pre-merge state)`. Agents were scored against an answer key computed
from a tree they could not see. The drift here is 1–2 files per task, so
it is not load-bearing for this pilot, but it means org-scale file-list
oracles are systematically off by whatever the merge commit touched.
Either build the key at the pinned commit or pin the workspace at the
key's commit.

> **Fixed (2026-08-05)** — the second option. Tasks now declare what their
> `ground_truth_commit` means: PR-derived tasks keep the parent pin (the
> agent reproduces the merge), while comprehension and org-scale producers,
> whose keys come from scanning that exact tree, pin the workspace there.
> `mining.multi_repo` needs both at once — its primary is a PR repro and its
> secondaries are scanned at HEAD — which is why the flag lives on the
> producer's metadata and on each `RepoRef` rather than on a task-type name.
> Suites mined before this change keep the old parent pin and must be
> re-mined.

### What would actually decompose this

Four arms over the same tasks, changing one variable at a time:

1. `baseline` — local tools, `instruction.md`
2. `baseline + mcp preamble` — local tools, `instruction_mcp.md`
   (isolates the preamble; cheapest arm, highest information gain)
3. `mcp loose` — local tools **and** MCP, `instruction_mcp.md`
   (augmentation: the question a Claude Code user actually has)
4. `mcp pragmatic` — MCP only (substitution: what run 5 measured)

Plus three changes to the suite itself:

- **Drop or regenerate `8faa5715`** before any rerun.
- **Report precision and recall separately.** F1 hid that the arms are
  at precision parity; the single headline number pointed at the wrong
  conclusion.
- **Switch to the multi-hop task variant.** `MIGRATION_INVENTORY` and
  `INCIDENT_DEBUG` both already declare `multi_hop=True` with a
  `multi_hop_description` ("find callers of deprecated symbols —
  requires tracing call sites, not just finding annotations"). That is
  the shape where `find_references` can beat grep. The flat
  pattern-enumeration variant structurally cannot show MCP benefit, no
  matter how the arms are configured — grep is the ceiling and it scores
  0.99.

Statistical footnote: n=4 valid tasks, 1 repeat, p=0.625. This design
cannot detect a difference of any size. "Baseline nominally ahead" was
over-stated even before the confounds.

### Reproducing the oracle validation

```python
# Run from the repo root (~/test_repos/kubernetes). Faithful to
# _filter_by_suffix + _scan_files: same suffix set, same vendor/
# node_modules/testdata exclusions, same 500-char line cap.
import re, subprocess, json
from pathlib import Path
SUF = {'.py','.go','.java','.ts','.js','.rs','.kt','.cpp','.c','.h','.rb'}
PATS = [re.compile(p) for p in (       # migration-inventory
    r"@[Dd]eprecated", r"#\[deprecated", r"//\s*Deprecated:",
    r"warnings\.warn\(.*[Dd]eprecat", r"\.warn\(.*[Dd]eprecat", r"@deprecated")]
tracked = subprocess.run(['git','ls-files'], capture_output=True, text=True).stdout.split('\n')
cands = [f for f in tracked if f
         and not any(s in f for s in ('vendor/','node_modules/','testdata/'))
         and any(f.endswith(s) for s in SUF)]
true = set()
for f in cands:
    try: lines = Path(f).read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError: continue
    if any(p.search(l) for l in lines if len(l) <= 500 for p in PATS):
        true.add(f)
exp = set(json.load(open('.codeprobe/tasks/8faa5715/ground_truth.json'))['expected'])
print(len(exp), len(true), len(exp & true))   # -> 7 152 0
```

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

- **One shipped answer key (`8faa5715`) is invalid**, and org-scale
  file-list keys are built at a different commit than the trial
  workspace. Both detailed under "Two codeprobe defects found while
  auditing".

Doesn't answer:

- **Whether Claude Code + Sourcegraph MCP helps at all**, on any task
  shape. Run 5 was initially read as answering this for
  file-discovery-by-marker; the confound audit withdraws that. The arms
  differed on preamble, tool surface, and access to the oracle's own
  generating function simultaneously, so no arm isolates the MCP's
  contribution.
- **How much of the run-5 gap is `pragmatic` isolation** versus the MCP
  itself. Untested: `loose` mode (blend MCP and local tools) was never
  run to completion on a valid suite.
- **Whether the MCP preamble helps or hurts.** It was applied only to
  the MCP arm, so its effect is fully confounded with tool surface.

Also newly answered:

- **Setting explicit `allowed_tools`/`disallowed_tools` on an MCP
  config silently opts it out of `mcp_mode`'s auto-policy entirely**
  (documented behavior in `mcp_policy.py`, confirmed the hard way in
  run 4). If you need to block one specific tool inside an auto-mode
  policy, that's exactly what the `9e38d21` fix now does automatically
  for `evaluator` — don't hand-roll it with `--disallowed-tools`, since
  doing so drops the Grep/Bash/Glob block along with it.

## Original followups and disposition

- **Resolved:** fix and document the `--timeout` vs. mined `time_limit_sec`
  precedence gap.
- **Not changed:** consider whether `codeprobe mine --goal mcp` should set a larger
  `time_limit_sec` by default for `org_scale_cross_repo` tasks
  spanning hundreds-to-thousands of files. This remains a separate policy
  decision; the confirmed precedence bug did not require it.
- **Resolved:** reran the same 5 tasks with true `mcp_mode=pragmatic` (no
  manual tool overrides) and `--pristine-config`. See "Post-fix
  validation" above for the result. Total additional spend: $10.14 (run
  4, discarded) + $27.93 (run 5, valid) = $38.07, bringing the pilot's
  grand total to **$69.48** across all 5 runs.

## Reproducer

```bash
# Mine (already done for this repo; suite at .codeprobe/suite.toml)
codeprobe mine ~/test_repos/kubernetes --json --no-interactive --goal mcp --count 5

# Configs already registered in ~/test_repos/kubernetes/.codeprobe/experiment.json:
#   baseline              — agent claude, no MCP
#   with-sourcegraph-mcp  — mcp_mode=pragmatic, instruction_mcp.md,
#                            allowed_tools/disallowed_tools left null so the
#                            9e38d21 auto-policy applies (blocks Grep/Bash/
#                            Glob AND mcp__sourcegraph__evaluator). Do not set
#                            --disallowed-tools manually here — see "Post-fix
#                            validation" above for why that defeats mcp_mode.

codeprobe run ~/test_repos/kubernetes \
  --agent claude --suite ~/test_repos/kubernetes/.codeprobe/suite.toml \
  --timeout 1200 --pristine-config --max-cost-usd 25 --parallel 5 --json
```

Run dir: `~/test_repos/kubernetes/.codeprobe/runs/`.
