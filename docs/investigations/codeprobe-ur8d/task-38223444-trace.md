# Task 38223444 — with-sourcegraph trace evidence

**Task:** *Find references to WriteFile in gascity*
**Pattern:** symbol-reference-trace
**Expected files (6):**
* `cmd/gc/controller_test.go`
* `internal/fsys/atomic.go`
* `internal/fsys/atomic_internal_test.go`
* `internal/fsys/fake.go`
* `internal/fsys/fake_test.go`
* `internal/fsys/fsys.go`

**with-sg result across 3 repeats:** recall = `{0.333, 0.333, 0.333}`,
stdev = `0.000`. Same 2 of 6 files matched every time.

## What the with-sg agent wrote

`answer.txt` (final result, captured from trace.db Write events on
`event_seq=16` and `event_seq=25`):

```
internal/fsys/fake.go
internal/fsys/fake_test.go
```

Agent's final-message reasoning (`event_type='result'`):

> Wrote `answer.txt` with the two files that reference `*Fake.WriteFile`
> **directly**: the definition site (`internal/fsys/fake.go`) and the
> direct caller (`internal/fsys/fake_test.go`).

The agent narrowed task scope to *direct callers of the method* and stopped.
The instruction explicitly asked for references "including through aliases,
re-exports, and wildcard imports" — the broader interface usages in
`atomic.go`, `atomic_internal_test.go`, `fsys.go`, and the wildcard-import
caller in `cmd/gc/controller_test.go` were not enumerated.

## Tool usage

| Config            | Total tool calls | Breakdown |
| ----------------- | ---------------: | --------- |
| baseline          |               72 | Grep ×45, Read ×13, Bash ×14 |
| with-sourcegraph  |               23 | `mcp__sourcegraph__keyword_search` ×13, `mcp__sourcegraph__read_file` ×7, `mcp__sourcegraph__nls_search` ×2, `mcp__sourcegraph__list_files` ×1 |

The with-sg agent makes ~3× fewer tool calls. Per-query cost is similar
(both are remote round-trips), but baseline's grep-everything pattern
naturally surfaces every textual occurrence of `WriteFile` in the repo,
including the broader interface usages that the structured sg queries
were too narrow to find.

## Sourcegraph query log (with-sg, all repeats)

```
seq=0  : repo:^github.com/gastownhall/gascity$ WriteFile file:internal/fsys/fake.go
seq=4  : repo:^github.com/gastownhall/gascity$ \.WriteFile\(
seq=5  : repo:^github.com/gastownhall/gascity$ WriteFile
seq=7  : repo:^github.com/gastownhall/gascity$ fs.WriteFile
seq=10 : repo:^github.com/gastownhall/gascity$ file:internal/doctor/checks_test.go fs :=
seq=12 : repo:^github.com/gastownhall/gascity$ fsys.NewFake
seq=13 : repo:^github.com/gastownhall/gascity$ f.WriteFile
seq=14 : repo:^github.com/gastownhall/gascity$ import . "github.com/gastownhall/gascity/internal/fsys"
seq=15 : repo:^github.com/gastownhall/gascity$ = fsys.Fake
seq=19 : repo:^github.com/gastownhall/gascity$ fs.WriteFile
seq=21 : repo:^github.com/gastownhall/gascity$ file:internal/doctor/checks_test.go fsys.OSFS
seq=22 : repo:^github.com/gastownhall/gascity$ fsys.NewFake
seq=24 : repo:^github.com/gastownhall/gascity$ Fake WriteFile lang:go
```

## Backend health check (was this a sourcegraph rate-limit / partial-result
artifact?)

No. Searched all `tool_output` records for the with-sg trials of 38223444:

* No occurrences of `rate`, `limit`, `error`, `failed`, or `timeout`.
* No "partial results" markers in any `keyword_search` response.
* Tool latency profile is consistent with normal operation (no
  retry/backoff loops).

The recall=0.333 result is **not** a backend issue. It is a deterministic
agent-behavior pattern: structured search with a precise index encourages
narrow, high-confidence answers; under recall reward, that asymmetry
shows up as under-shipping.

## Why this matters

This is the textbook codeprobe-voxa case: tool-shape (precision/recall
trade-off) was previously rewarded as if it were quality. Under F1 the
with-sg agent looks tighter — recall=0.333 with precision=1.0 yields
F1=0.5 vs baseline's recall≈0.83 with precision≈0.30 yielding F1≈0.43.
Under recall the right framing emerges: with-sg solved 1/3 of the task,
baseline solved most of it. The sourcegraph integration isn't broken;
the agent just over-trusts a narrow keyword index and stops searching.

## Limitations of this trace

`trace.db` has primary key `(run_id, config, task_id, event_seq)` so
events from multiple repeats with overlapping `event_seq` overwrite
each other — only the last repeat's events survive. The two `result`
events captured here cover the two repeats that wrote distinct
`event_seq` ranges; the third repeat's tool sequence is not preserved
in the database. The scoring summary
(`per_trial.json`) confirms all three repeats produced the same
recall=0.333 result, so the qualitative finding (narrow scope, no
backend errors) is representative even though one repeat's tool log is
not on disk.
