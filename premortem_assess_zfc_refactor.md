# Premortem: Assess ZFC Refactor

## Risk Registry

| #   | Failure Lens             | Severity     | Likelihood | Score  | Root Cause                                                                         | Top Mitigation                                                                        |
| --- | ------------------------ | ------------ | ---------- | ------ | ---------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| 1   | Technical Architecture   | Critical (4) | High (3)   | **12** | Dynamic dimensions make scores incomparable across repos and runs                  | Fixed rubric — model scores against predefined dimensions, doesn't invent them        |
| 2   | Scope & Requirements     | Critical (4) | High (3)   | **12** | Optimized for elegance over user need (repeatable, comparable, automatable scores) | Cap scope to replacing scoring engine only; fixed dimensions, defer dynamic discovery |
| 3   | Integration & Dependency | Critical (4) | High (3)   | **12** | Tight coupling to unversioned `claude -p` JSON envelope via raw dict key access    | `CLIEnvelope` dataclass with strict validation; golden snapshot contract test         |
| 4   | Operational              | Critical (4) | High (3)   | **12** | Model and heuristic paths produce structurally different `AssessmentScore` shapes  | Both paths return identical shape; `scoring_method` discriminator field               |
| 5   | Scale & Evolution        | High (3)     | High (3)   | **9**  | `call_claude` too thin for reuse — no caching, batching, cost tracking             | `LLMRequest`/`LLMResponse` abstractions; content-addressed cache; cost accumulator    |

## Cross-Cutting Themes

### Theme 1: Dynamic dimensions is the wrong design (Lenses 1, 3, 4)

Three independent agents converged on the same conclusion: letting the model pick scoring dimensions per-repo breaks the tool's core use case. Users need to compare repos side-by-side, integrate scores into CI, and reason about what to improve. Variable dimension sets make all of this impossible. The scope agent correctly identified that the original problem (cliff thresholds giving 100%) is a **calibration** problem, not a **dimensions** problem.

**Verdict:** Fixed rubric with model-assigned scores. The model judges _how good_ each dimension is, not _what dimensions exist_.

### Theme 2: Output shape mismatch between model and heuristic paths (Lenses 1, 3, 4)

If `score_repo_with_model()` and `score_repo_heuristic()` return different shapes, every downstream consumer (CLI rendering, ELO module, CI scripts) breaks depending on which path ran. The operational agent flagged that silent fallback + different shapes = corrupted datasets. The scope agent noted the heuristic path will bit-rot if it's a second-class citizen.

**Verdict:** Both paths MUST return an identical `AssessmentScore` — same dimensions, same field names. Add `scoring_method: Literal["model", "heuristic"]` so consumers know provenance without checking structure.

### Theme 3: Envelope parsing fragility (Lenses 1, 2, 3)

The `claude -p --output-format json` envelope is unversioned and uncontracted. All three agents independently predicted the same failure: Anthropic ships a CLI update, key names change, `core/llm.py` breaks silently or loudly. The deps agent specifically called out that `response["result"]` access with no validation lets malformed responses sail through.

**Verdict:** Parse into a strict `CLIEnvelope` dataclass immediately. Golden snapshot contract test. Version-sniff `claude --version` and warn on untested versions.

### Theme 4: Fallback path must be first-class (Lenses 1, 2, 3, 4)

Four of five agents flagged that the heuristic fallback, despite being specified as a graceful degradation, would receive less testing and maintenance attention, eventually drifting out of sync with `AssessmentScore` schema changes.

**Verdict:** Shared construction path — both scoring methods go through the same factory/validation. Both have dedicated test coverage. Fallback triggers must be broad: missing binary, non-zero exit, timeout, bad JSON, missing keys, error envelopes.

### Theme 5: `core/llm.py` needs abstractions for reuse (Lenses 1, 5)

The utility is explicitly designed for reuse by `mine`, `interpret`, and future commands. A bare `call_claude(prompt) -> dict` function can't support streaming, batching, caching, cost tracking, or retry logic that those commands will need.

**Verdict:** `LLMRequest`/`LLMResponse` dataclasses from day one. Content-addressed cache (`hash(prompt + model) -> response`). Cost tracking via token counts on `LLMResponse`. Don't over-build — but don't paint into a corner.

## Mitigation Priority List

| Priority | Mitigation                                                                                          | Failure modes addressed | Effort |
| -------- | --------------------------------------------------------------------------------------------------- | ----------------------- | ------ |
| **P0**   | Fixed rubric (model scores predefined dimensions, doesn't invent them)                              | 1, 2, 3, 4              | Low    |
| **P0**   | Identical `AssessmentScore` shape from both paths + `scoring_method` field                          | 1, 3, 4                 | Low    |
| **P0**   | Strict `CLIEnvelope` validation in `core/llm.py` — parse-then-validate, not raw dict access         | 1, 2, 3                 | Low    |
| **P1**   | Broad fallback triggers (timeout, bad JSON, error envelope, missing keys — not just missing binary) | 2, 3                    | Low    |
| **P1**   | `LLMRequest`/`LLMResponse` dataclasses with token count, latency, model fields                      | 1, 5                    | Medium |
| **P1**   | Golden snapshot contract test for Claude CLI envelope                                               | 2, 3                    | Low    |
| **P1**   | Integration test with fake `claude` shell script on PATH                                            | 2, 3                    | Low    |
| **P2**   | Content-addressed response cache in `.codeprobe/cache/`                                             | 5                       | Medium |
| **P2**   | `claude --version` sniff + compatibility warning                                                    | 2                       | Low    |
| **P2**   | Cost accumulator context manager with per-session summary                                           | 5                       | Medium |
| **P3**   | `--max-concurrent` flag for batch mode with backoff on 429s                                         | 5                       | Medium |
| **P3**   | Streaming support stub in `core/llm.py` interface                                                   | 1, 5                    | Low    |

## Design Modification Recommendations

### 1. Fixed rubric, model scores against it (P0)

Define 5-6 canonical dimensions as a `RUBRIC_V1` constant. The prompt sends raw `RepoHeuristics` stats + rubric definition. The model returns a score + reasoning per dimension. The model may suggest weights, but the dimensions are fixed.

**Addresses:** Lenses 1, 2, 4 (comparability, repeatability, automation)
**Effort:** Low — simplifies the `DimensionScore` design, not complicates it

### 2. Strict envelope parsing + golden snapshot test (P0)

`CLIEnvelope` dataclass in `core/llm.py`. Parse raw JSON into it immediately. Validate required fields. Golden snapshot test captures a real envelope and asserts the parser handles it. Fallback triggers on any validation failure.

**Addresses:** Lenses 2, 3 (envelope breakage, silent corruption)
**Effort:** Low

### 3. Identical output shape with `scoring_method` discriminator (P0)

Both `score_repo_with_model()` and `score_repo_heuristic()` return the same `AssessmentScore` with the same `DimensionScore` tuples for the same rubric dimensions. Only `scoring_method` differs.

**Addresses:** Lenses 1, 3, 4 (shape mismatch, fallback bit-rot)
**Effort:** Low

### 4. `LLMRequest`/`LLMResponse` abstractions (P1)

Not a full framework — just frozen dataclasses that carry prompt, model, timeout on the way in, and text, tokens, cost, latency on the way out. The subprocess shell-out stays the same. But callers compose with typed objects, not raw dicts.

**Addresses:** Lenses 1, 5 (reuse, extensibility)
**Effort:** Medium

### 5. Content-addressed cache (P2)

`hash(prompt + model) → cached LLMResponse` in `.codeprobe/cache/`. Repo stats change rarely; re-running `assess` on the same repo should be free. TTL-based expiry.

**Addresses:** Lens 5 (cost at scale)
**Effort:** Medium — defer to after core pipeline works

## Full Failure Narratives

### Lens 1: Technical Architecture Failure (Critical / High)

The dynamic dimensions design made scores incomparable across repos and runs. The `core/llm.py` envelope parsing broke silently when Anthropic updated the CLI, returning zeroed-out scores that looked like real assessments. The fallback never triggered because it only checked for subprocess failure, not schema-invalid output. The 229 tests all passed because they mocked the model path. The `core/llm.py` utility, designed for reuse, turned out to be too narrow for `mine` and `interpret` which needed streaming and multi-turn calls, leading to a second competing LLM interface.

### Lens 2: Integration & Dependency Failure (Critical / High)

Claude CLI v2.0 renamed envelope keys (`result` → `content`, `total_cost_usd` moved inside `billing`). `core/llm.py` threw `KeyError` on every call. The fallback activated but had drifted out of sync with the evolved `AssessmentScore` schema, producing `TypeError` on construction. All three commands (`assess`, `mine`, `interpret`) broke simultaneously because they shared `core/llm.py`. No contract test existed to detect the envelope change. The hotfix took four days of reverse-engineering.

### Lens 3: Operational Failure (Critical / High)

Users in CI/Docker without Claude CLI got silent heuristic fallback with structurally different output. Users with misconfigured Claude got error envelopes that passed the exit-code check but broke parsing. Latency of 3-8 seconds per assessment with no progress indicator led to Ctrl-C kills. Mixed model/heuristic scores in batch runs corrupted downstream ELO rankings silently.

### Lens 4: Scope & Requirements Failure (Critical / High)

Dynamic dimensions broke CI integrations and cross-repo comparison — the tool's primary use case. The original problem (cliff thresholds) was a calibration issue, not a dimensions issue. The heuristic fallback bit-rotted within two months. The team spent three months triaging `assess` regressions instead of building `mine` and `run`.

### Lens 5: Scale & Evolution Failure (High / High)

120-repo batch CI took 16 minutes with no parallelism. Naive parallelization hit 429 rate limits. Silent heuristic fallback corrupted ELO tournaments. Monthly CI costs hit $750 with no cost dashboard. `call_claude`'s thin interface couldn't support caching, batching, or streaming without a rewrite that broke all callers.
