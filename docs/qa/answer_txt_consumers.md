# `answer.txt` consumers — authoritative vs hint-only audit

**Source bead:** codeprobe-w8pg (parent: dr-2vydrm cluster `answer_txt_drift`).

The shipped `answer.txt` in a task directory has two roles depending on
context. This audit catalogs every consumer, classifies its trust level,
and notes which consumers are at risk when `answer.txt` drifts from
`ground_truth.json`.

## TL;DR

* **No consumer treats curator-shipped `answer.txt` as authoritative.**
  After codeprobe-9fri (`synthesize_golden_output`), `ground_truth.json`
  is the only canonical answer source.
* **Most run-time consumers read `answer.txt` as the *agent's* output**,
  not the curator's reference. Drift is invisible to those because the
  agent overwrites the file before scoring.
* **The remaining risk is offline calibration** (where the agent never
  runs and the shipped `answer.txt` is the only signal) and **fixture
  hygiene** (where downstream tools could regress to reading shipped
  `answer.txt` directly). codeprobe-9fri locked the calibration triad
  to ground-truth-first; the validate-time finding (codeprobe-w8pg)
  now prevents a future regression by surfacing drift at curation time.

## Consumer table

| Consumer | File:line | Trust level | Notes |
|---|---|---|---|
| `synthesize_golden_output` | `src/codeprobe/calibration/triad.py:223` | **hint-only** (post codeprobe-9fri) | Reads `answer.txt` only when `ground_truth.json` lacks every answer-bearing field. Stale `answer.txt` is harmless because ground_truth wins. |
| `_strip_answer_files` | `src/codeprobe/calibration/triad.py:297-317` | **defensive** | Strips `answer.txt` from a temp-copied task dir before running fixtures so the shipped curator copy can't leak into oracle.py's `$AGENT_OUTPUT` path. |
| `Executor` answer staging | `src/codeprobe/core/executor.py:489-503` | **agent output** | Looks for the file the *agent wrote* into the working repo; copies it into the scoring sandbox. Curator-shipped `answer.txt` in the task dir never reaches this path. |
| `_STALE_ANSWER_FILES` cleanup | `src/codeprobe/core/executor.py:61` | **defensive** | Lists `("answer.txt", "answer.json", "reward.txt")` for pre-run sanitization in the executor so a stale file from a previous run cannot bleed into the next agent. |
| Oracle `oracle.py` template | `src/codeprobe/probe/writer.py:124-128`, `src/codeprobe/mining/writer.py:1688,1793-1795` | **agent output** | The `oracle.py` template prefers `$AGENT_OUTPUT`, falls back to `answer.txt` *only* when the agent skipped writing the env-var path. This is the agent's run-time output, not the curator hint. |
| `extract_answer` (org-scale) | `src/codeprobe/mining/org_scale_oracle.py:64-118` | **agent output** | Run-time helper that parses the agent's `answer.txt` for `file_list` / `count` / `boolean` oracle types. Curator-shipped file is unused at run time. |
| R17 checkpoint | `src/codeprobe/mining/org_scale.py:59-102` | **agent output** | bash check that the agent produced a non-empty `answer.txt`. Stale curator file would short-circuit the check incorrectly only if the executor failed to clean it up — the `_STALE_ANSWER_FILES` defensive cleanup above guards against that. |
| Comprehension oracle | `src/codeprobe/mining/comprehension.py:65` | **N/A** | Comprehension tasks are `artifact_eval` and explicitly do not read `answer.txt`; documented as such in the source comment. |
| `validate <task_dir>` drift check | `src/codeprobe/cli/validate_cmd.py` (codeprobe-w8pg) | **curation-time gate** | New: compares curator-shipped `answer.txt` against `ground_truth.json`'s answer field under answer-type-aware normalization; emits a `warn:` finding on mismatch. |

## Where drift can still bite

1. **Calibration with insufficient ground truth.** Tasks whose
   `ground_truth.json` lacks `answer`, `expected`, and `checks[*].answer`
   fall through to `answer.txt` (`synthesize_golden_output` step 2). A
   stale file there *will* be used for the golden fixture. Mitigation:
   the validate-time drift finding flags these before they reach the
   triad, so curators can either remove the stale `answer.txt` or
   reconcile it.
2. **Downstream tooling outside this repo.** Any analysis script that
   reads `task_dir/answer.txt` directly (instead of going through the
   triad/executor pipeline) will see the stale data. None of the
   in-repo consumers do this any more — the audit above is the
   authoritative list.

## Acceptance trace

* Audit table committed to this file (codeprobe-w8pg, AC1).
* `codeprobe validate <task_dir>` emits a `warn:` finding when
  `answer.txt` disagrees with `ground_truth.json` under the
  appropriate normalization (codeprobe-w8pg, AC2).
* `e2e-codeprobe-self/tasks/0f2b0737/answer.txt` reconciled with its
  ground truth — see commit log for codeprobe-w8pg (codeprobe-w8pg,
  AC3).
