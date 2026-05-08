# codeprobe-jf28 — ttwq rerun (oc_004 regression fix)

## Question

`codeprobe-ttwq` (May 2026) found that with-sourcegraph at N=3 lost on
`oc_004` specifically: 12/15 perfect, mean reward 0.914 vs baseline's
1.000. The failure mode was confident denial — every with-sg trial
declared *"`FlagAliases` does not exist in the gascity codebase"* even
though `FlagAliases` is declared at
`internal/config/provider.go:34` and used across the codebase.

`codeprobe-jf28` shipped two interventions targeting exactly this
failure mode:

1. **v2 preamble** with explicit "Verify before denying existence"
   guidance in the oracle_checks `workflow_tail` (and the rule that
   the rubric guarantees the named symbol exists).
2. **File-removal isolation** — local source absent, agent commits
   to MCP queries instead of half-attempted local fallbacks.

Plus the `/all` endpoint switch (broader index coverage than `/v1`).

This rerun replicates ttwq's setup with the new `with-sg-isolated`
config swapped in for the v1 `with-sourcegraph` config. Same 5 oracle
tasks, same N=3, same baseline.

## Result

**The oc_004 regression is gone. 30/30 trials scored 1.0.**

| Config | n | mean | std | perfect | total cost |
|---|---|---|---|---|---|
| baseline | 15 | 1.000 | 0.000 | 15/15 | $4.40 |
| **with-sg-isolated** | 15 | **1.000** | **0.000** | **15/15** | **$4.06** |

### Side-by-side with ttwq

| | ttwq (v1, May 2026) | jf28 rerun (v2 + isolated) |
|---|---|---|
| baseline mean | 1.000 (15/15) | 1.000 (15/15) |
| with-sg mean | **0.914 (12/15)** | **1.000 (15/15)** |
| oc_004 with-sg | [0.643, 0.429, 0.643] | [1.0, 1.0, 1.0] |
| Family delta | −0.0857 (p ≈ 0.092) | 0.000 |
| Failure mode | "FlagAliases does not exist" × 3 | n/a |

### oc_004 sample answer (with-sg-isolated rerun)

> *"It is a field on the `OptionChoice` struct, defined in
> `internal/config/provider.go` at line 34: `FlagAliases [][]string
> \`toml:"flag_aliases,omitempty" json:"-"\``"*

> *"`CollectAllSchemaFlags` (in `internal/config/options.go`) delegates
> to `choiceFlagSequences`, which appends both `choice.FlagArgs` and
> every `choice.FlagAliases` entry."*

The agent correctly cites the provider.go declaration, the toml tag,
the resolve-time code path (`specToResolved` in
`internal/config/resolve.go:522`), and the alias normalization helper.
The cited line numbers match the actual gascity codebase; this
behavior is what ttwq's three with-sg trials all failed to produce.

## Tool usage

| Config | Total calls | Breakdown |
|---|---|---|
| baseline | 254 | 101 Read, 75 Bash, 62 Grep, 10 Agent, 6 Glob |
| with-sg-isolated | 169 | 81 keyword_search, 63 read_file, 11 list_files, 5 commit_search, 4 compare_revisions, 3 nls_search, 2 diff_search |

**With-sg-isolated uses 33% fewer tool calls than baseline overall and
matches or beats it on every per-task call count except oc_001 and
oc_004**. Of note: with-sg-isolated reaches for `nls_search` (3 calls)
and `compare_revisions` (4 calls) in this rerun — both unused in the
3-way 1-rep run earlier. The v2 preamble's "If Stuck" section (which
explicitly recommends `sg_nls_search` for stem-form fallback) is
visible in the trace.

## Cost

- N=3 total: $8.46 (well under the $18 cap)
- baseline mean per trial: $0.293
- with-sg-isolated mean per trial: $0.271 (-7.5%)

The cost gap is smaller than the 1-rep 3-way's 47% — at N=3 the
prompt cache amortizes more effectively across baseline trials, so
baseline's $/trial drops disproportionately.

## What this confirms

- **The "FlagAliases denial" regression that ttwq pinned at p ≈ 0.092
  no longer reproduces under v2 + isolation.** All three originally-
  failing trials are now passes.
- **The "Verify before denying existence" preamble rule is doing
  work.** The oc_004 agent in this rerun explicitly broadens its
  query rather than concluding the symbol is absent.
- **MCP coverage at the `/all` endpoint is sufficient.** Whether the
  underlying index improvement, the endpoint change, the preamble, or
  the isolation is the dominant cause is not separately identified
  here — the rerun is end-to-end. To attribute the win, run the
  matching with-sg-fixed at N=3 on oc_004 (next followup if useful).
- **Quality is still saturated.** As in the 3-way 1-rep, oracle_checks
  ceilings out at 1.0 once the failure mode is fixed. To push the
  comparison further you need a category where the rubric isn't easy
  to satisfy in full — symbol-reference-trace at high recall or SDLC
  with the `with-sg-fixed` config (since SDLC tasks need source).

## Reproducer

```bash
codeprobe run ~/test_repos/gascity/gascity-jf28-ttwq-rerun/.codeprobe \
  --timeout 900 --parallel 5 --repeats 3 --max-cost-usd 18 --force-plain
codeprobe interpret ~/test_repos/gascity/gascity-jf28-ttwq-rerun/.codeprobe
```

Run dir: `~/test_repos/gascity/gascity-jf28-ttwq-rerun/.codeprobe/runs/`
