# ZFC compliance — codeprobe

codeprobe is AI-orchestration code, so ZFC applies at **two levels**:

1. **L2 (tooling):** codeprobe's own orchestration code must not use heuristics for semantic judgment.
2. **L3 (product):** defaults and heuristics embedded in codeprobe shape how users perceive their benchmarks.

A heuristic in codeprobe doesn't just affect codeprobe — it shapes how users perceive their domain through codeprobe's lens.

## Compliant code (mechanism, not policy)

| File | Why it's compliant |
| --- | --- |
| `core/scoring/` | Delegates pass / fail to `test.sh` (gold-standard ZFC); IR scorers report reward = recall (or `weighted_recall` for tiered oracles) with precision / F1 in `ir_metrics`. The split is documented arithmetic, not judgment — see `docs/scoring_model.md` |
| `core/llm.py` | Shared Claude CLI utility for model-based judgment (pure IO + mechanical parsing) |
| `analysis/ranking.py` | Deterministic arithmetic with explicit tiebreakers |
| `analysis/trace_quality.py` | Mechanical projection of `CompletedTask` + `BiasWarning` records onto a per-trial quality view; sole threshold (`LOW_RECALL_THRESHOLD`) is a documented constant that surfaces an existing oracle metric, not a quality verdict (see `docs/trace_quality.md`) |
| `adapters/` | Mechanical parsing, honest about data quality via `cost_source` |
| `analysis/stats.py` | Arithmetic aggregation (deterministic math, not judgment) |
| `snapshot/evidence_*.py` | Closed-schema parsing, structural independence gates, deterministic arithmetic, content-bound approval, and atomic export. Representativeness and the bounded conclusion are explicit data-owner inputs; code does not infer meaning from prose |
| `assess/heuristics.py:score_repo_with_model()` | Delegates scoring to Claude via fixed `RUBRIC_V1`; model judges quality, code does IO |
| `mining/extractor.py:generate_instruction()` | Delegates instruction.md generation to LLM; regex fallback only for `--no-llm` offline mode |
| `mining/curator_tiers.py:verify_curation()` | Delegates the overall pass / warn / fail curation verdict to the model; application code only samples files, validates complete structured output, records explicit unavailable/error states, and enforces the admission gate |
| `config/defaults.py` narrative-source resolver | Delegates selection to `core/llm.py` under fixed rubric `_NARRATIVE_RUBRIC_V1`; falls back to deterministic priority `pr > commits > rfcs > issues` only when no LLM backend is available or `offline=True`; emits an `LLM_UNAVAILABLE` envelope warning so callers see the degraded mode (PRD §13-T4 refactor) |

## Known violations (tracked for refactoring)

- `mining/extractor.py:358-366` (`_estimate_difficulty()`) — file-count difficulty estimation (≤3 → easy, >10 → hard). A 20-file rename is "hard" while a critical 1-file security fix is "easy". Replace with model-assessed difficulty or user-provided metadata.
- `assess/heuristics.py:_detect_test_frameworks()` — regex framework detection. Structural file-glob part is OK, but "does this repo have good test coverage?" is semantic — delegate to model.
- `cli/mine_cmd.py:_quality_review()` — three heuristics: length+keyword check for "thin instructions" (desc < 50 chars), hardcoded `0.7` threshold for "low diversity", stub-command keyword match. These are UI warnings, not scoring judgments, so lower priority for refactoring.
- `mining/org_scale_families.py` — `min_hits` thresholds (3-5) are hardcoded. Structural file-counting is OK per ZFC, but the thresholds are arbitrary. Acceptable as tunable parameters.
- `mining/curator_tiers.py:assign_ground_truth_tiers()` — the `use_llm=False` branch (line ~410) returns the pure mechanical heuristic tiers without any LLM call. This is a documented offline fallback mode; callers that opt in accept the ZFC trade-off. Not a drift bug — refactor would instead tighten the docstring / labeling so consumers know when they're seeing heuristic-only tiers.

## Justified exceptions

- `assess/heuristics.py:score_repo_heuristic()` — the disclosed, labeled fallback twin of `score_repo_with_model()` (listed above as compliant). When the Claude CLI is unavailable or the model call fails, repo benchmarking potential is scored by a fixed threshold ladder over structural git/file metadata (`merge_commits`, `total_files`, `has_tests`, …) under the same `RUBRIC_V1` dimensions. This IS coded semantic judgment, accepted because: (1) it is only reached on model-unavailability, never preferred; (2) every result is stamped `scoring_method="heuristic"` / `model_used=None`, and `AssessmentScore.__post_init__` validates `scoring_method` against `ALLOWED_SCORING_METHODS` so a score can never be presented without a known method label; (3) both render paths (`cli/assess_cmd.py` pretty + envelope) emit `scoring_method` alongside `overall`. Refactor option if model coverage becomes guaranteed: drop the ladder and surface "assessment unavailable (offline)" instead. (codeprobe-b9c #2)
- `analysis/stats.py` — arithmetic aggregation is deterministic math, not judgment.
- Secret-redaction regex in `scoring.py` — pattern matching for known token formats is structural, not semantic.
- `core/isolation.py:_collect_scaffold_paths` / `_collect_overlay_files` (codeprobe-2nw2 scaffold mode) — `TRUNCATE_EXTENSIONS` allowlist + path-prefix excludes (`.git/`, `tests/`, `.codeprobe/`, `.claude/`, `.github/workflows/`) for sg-only scaffolding. Pure structural file-system metadata comparison (suffix membership, size > 0, prefix match against a manifest captured at context-manager entry) with no semantic judgment about file content. See `docs/investigations/codeprobe-2nw2/design.md` §ZFC compliance note.
- `adapters/claude.py:_QUOTA_PATTERN` / `_AUTH_FAILURE_PREFIX` (codeprobe-9xrl; session-limit variant added in codeprobe-4cl6.3) — vendor transport strings (`monthly usage limit`, `rate limit exceeded`, `quota exhausted`, `hit your session limit`, and the exact `Not logged in` prefix) classify CLI failure stubs so the executor can route quota or authentication failures instead of scoring a stub as a 0.0 agent failure. Matching is restricted to literal CLI output rather than agent-authored stream events. This is blast-radius / policy enforcement at a trust boundary, not semantic judgment about agent output — but it IS brittle to vendor rewording, which is why it lives here. The model-delegated alternative (classify every error string via LLM) would add a model call to every failed trial for a closed transport-error enum.

## When to update this file

Update when: new heuristic code is introduced; a known violation is refactored to use model calls; a new justified exception is added.

NOT per-commit — only when the heuristic landscape changes.
