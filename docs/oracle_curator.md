# Oracle Curator — Multi-Backend Ground-Truth Construction

> Implements codeprobe-zat9 (mitigation for the single-tool oracle bias
> surfaced by the gascity MCP-vs-baseline rerun, codeprobe-wo7n).

The oracle curator builds the per-file answer key for symbol-reference-trace
and change-scope-audit tasks from **multiple search backends** instead of a
single tool. When the agent's MCP surface contains the same backend that
built the ground truth, scoring becomes tautological — the curator
structurally prevents this by requiring agreement across backends and by
recording per-task backend provenance so downstream bias-warning code can
detect when an agent's surface is a strict subset of the consensus set.

## Where it sits in the pipeline

```
mine_org_scale_tasks
  └─ _mine_symbol_reference_tasks   (or _mine_change_scope_tasks)
       └─ _consensus_ground_truth
            ├─ compute_consensus     ← F1 gate, decides shipped/quarantined
            └─ curate_consensus      ← per-file answer key + tier assignment
                 └─ _curate_with_llm  ← tier-2 LLM curator pass
```

`compute_consensus` (in `mining/consensus.py`) is the **task-level** gate:
it runs every configured backend, computes pairwise F1 between the file
sets they returned, and either ships the candidate or routes it to
`tasks_quarantined/` for a human review.

`curate_consensus` (in `mining/oracle_curator.py`) is the
**per-file** decision pass that runs **only when the gate passes**. It
ignores the intersection / union choice — those are coarse — and applies
the tiered rules below to each file independently.

## Tier rules

For every candidate file path reported by at least one available backend:

| # backends finding it      | Decision                                           | Tier label      |
| -------------------------- | -------------------------------------------------- | --------------- |
| `>= min_backends` (def. 2) | **Tier 1.** Keep without LLM call.                 | `required`      |
| exactly `1`                | **Tier 2.** LLM curator votes keep / reject.       | `supplementary` |

The default `min_backends=2` matches the bead spec "consensus ≥ 2"; it can
be raised to `3` to require unanimous agreement across all three default
backends (grep + ast + sourcegraph) at the cost of recall.

### Tier-2 LLM curator

When a file is reported by only one backend the curator delegates the
keep / reject judgment to a model call (default Haiku via
`core.llm.call_claude`). The prompt contains:

- the symbol name and defining file
- the candidate file path
- which single backend reported it
- a bounded snippet of the candidate file (≤ 80 lines, ≤ 8000 bytes)

The model must respond with strict JSON:
`{"keep": true|false, "rationale": "<one sentence>"}`. Any deviation
(non-JSON, missing field, wrong type) is treated as an error and the
vote is forced to `keep=false` — the conservative path quarantines the
candidate so a precision-leaning answer key is preferred over a
recall-leaning one when the curator cannot reach a decision.

Approved tier-2 items are kept as `tier="supplementary"` (lower weight
in the vendored weighted-F1 oracle than tier-1's `required`) and carry
`via_llm_review=true` plus the model's rationale in the divergence
report so reviewers can audit decisions after the fact.

### Single-backend fallback

When **only one backend is available** (`available == 1`) — for example
in a ripgrep-only environment without Sourcegraph auth — the curator
takes a documented offline path:

- skip the consensus filter entirely
- skip the LLM curator entirely
- keep every reported file as `required`

This preserves the current behavior in the single-backend case while
recording `oracle_backends_consensus = ["grep"]` so downstream code knows
the answer key was not in fact consensus-curated. Bias-warning logic
must treat a single-backend consensus as **not bias-mitigated**.

## ZFC compliance

| Aspect                                             | Mechanism                                                                                                        | Why this is ZFC-compliant                                                                                              |
| -------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| Tier-1 selection                                   | Count distinct backends per path, compare to `min_backends`.                                                     | Arithmetic; no semantic threshold. The threshold itself is a calibration knob exposed in `oracle_curator.py`.          |
| Tier-2 keep/reject                                 | LLM curator call; structural JSON parse.                                                                         | Decision is fully delegated to the model. Code only does IO (snippet read), HTTP (call_claude), and structural parse.  |
| `oracle_backends_consensus`                        | Union of backend names across kept items.                                                                        | Set arithmetic; no judgement.                                                                                          |
| Single-backend fallback                            | Bypass the curator entirely; keep all as `required`.                                                             | Documented offline mode — the operator opted in by configuring only one backend, so there is no ZFC drift.             |

This mirrors the boundary used elsewhere in the codebase (e.g.
`mining/curator_tiers.py::assign_ground_truth_tiers`): mechanical
graph-distance heuristic followed by a model refinement, with the model
getting the final say.

## ground_truth.json contract

A symbol-reference-trace task curated through the consensus path emits:

```json
{
  "schema_version": 2,
  "oracle_type": "file_list",
  "expected": ["path/a.py", "path/b.py", "path/c.py"],
  "commit": "deadbeef…",
  "pattern_used": "symbol-reference-trace",
  "repo": "myrepo",
  "oracle_tiers": {
    "path/a.py": "required",
    "path/b.py": "required",
    "path/c.py": "supplementary"
  },
  "curation": {
    "backends_used": ["grep", "ast", "sourcegraph"],
    "file_count": 3
  },
  "oracle_backends_consensus": ["ast", "grep", "sourcegraph"]
}
```

Field-by-field:

- **`oracle_tiers`** — per-file tier label that drives the vendored
  weighted-F1 oracle (`tests/oracle.py`). Files at `required` carry
  weight 2.0; `supplementary` carry 1.0. Untiered files default to
  `context` (weight 0.5) but are never emitted by the curator.
- **`curation`** — pre-existing curation provenance summary
  (mining/writer.py). The curator path populates `backends_used` from the
  `_run_curation` pipeline; the new `oracle_backends_consensus` field
  below is a stricter, semantic field that downstream bias-warning code
  reads.
- **`oracle_backends_consensus`** — sorted list of backend names that
  contributed at least one kept item to the answer key. Used by
  `core/bias_detection.py` to decide whether the agent's MCP surface is a
  strict subset (no tautology), an overlap (informational warning), or
  identical (full tautology). When the field is missing the task was not
  built through the consensus path — bias-warning code must fall back to
  the legacy detection rules and may not assume any structural
  protection.

The same schema applies to `change-scope-audit` tasks.

## divergence_report.json contract

Each shipped consensus task drops a `divergence_report.json` next to its
`ground_truth.json`. The curator extends the consensus report with its
own block:

```json
{
  "schema_version": "consensus.v1",
  "decision": "shipped",
  "backend_results": [...],
  "pair_metrics": [...],
  "consensus_files": [...],
  "curator": {
    "schema_version": "oracle_curator.v1",
    "min_backends": 2,
    "llm_used": true,
    "n_items": 3,
    "n_quarantined": 1,
    "quarantined": [
      {"path": "spurious.py", "reason": "LLM rejected: unrelated mention in a docstring"}
    ],
    "items": [
      {
        "path": "path/a.py",
        "backends": ["grep", "ast", "sourcegraph"],
        "tier": "required",
        "via_llm_review": false,
        "llm_rationale": ""
      },
      {
        "path": "path/c.py",
        "backends": ["sourcegraph"],
        "tier": "supplementary",
        "via_llm_review": true,
        "llm_rationale": "imports Foo via re-export from parent package"
      }
    ]
  }
}
```

Quarantined tier-2 candidates are kept in the report (with reasons) so
later reviewers can audit the curator's decisions without re-running the
miner.

## Configuration

Curator behavior is driven by the existing `--consensus-backends` and
`--consensus-mode` CLI flags (see `codeprobe mine --help`) plus the
new internal knobs:

| knob                  | location                                       | effect                                                      |
| --------------------- | ---------------------------------------------- | ----------------------------------------------------------- |
| `min_backends`        | `oracle_curator.curate_consensus`              | Files agreed by ≥ this many distinct backends are tier-1.   |
| `use_llm`             | `oracle_curator.curate_consensus`              | When False, tier-2 candidates are quarantined.              |
| `llm_timeout_seconds` | `oracle_curator.curate_consensus`              | Per-tier-2-call cap; default 30s.                           |

`_consensus_ground_truth` forwards `use_llm = not no_llm` so the
existing `--no-llm` mining flag deterministically disables the curator's
LLM path while keeping tier-1 (mechanical) selection.

## Reference patterns

- **CodeScaleBench.** `docs/migration_eval/oracle_funnel.md` describes
  the cheapest-first oracle funnel
  (compile → tests → ast → LLM judge → daikon). Codeprobe's curator is
  the symbol-reference equivalent — the funnel is shorter (multi-backend
  search → LLM tier-2 review) but the principle (cheap mechanical pass
  first, model only on disagreement) is identical.
- **EnterpriseBench.** `scripts/validation/enable_llm_curator.py` and
  `scripts/validation/validate_llm_curator_modes.py` opt tasks into a
  `verification_modes = ['deterministic', 'llm_curator']` set so
  reviewers can compare deterministic vs LLM-curated outcomes side by
  side. Codeprobe's curator is invoked at construction time rather than
  validation time, but the dissent-surfacing pattern is the same: the
  divergence report carries every disagreement so it can be re-litigated
  later.
