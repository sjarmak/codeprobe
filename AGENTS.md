# codeprobe agent guide

Codeprobe is a Python evaluation framework for comparing coding agents on
quality, cost, and speed. Application packages use the `src/codeprobe/` layout.
This file is the canonical repository guidance; `CLAUDE.md` points here instead
of maintaining a second copy.

## Start here

- Use Beads for durable work state: `bd prime`, `bd show <id>`, and
  `bd update <id> --claim`.
- Re-derive active epics with `bd list --type epic --status open`; do not trust
  an epic name copied into onboarding prose.
- Put exact paths/functions, numbered steps, code shapes, dependencies, tests,
  and acceptance criteria in every implementation bead.
- Architecture overview: `docs/onboarding/architecture_tour.md`.
- Adapter contract: `docs/adapters.md`.
- Scoring contract: `docs/scoring_model.md`.
- ZFC inventory: `docs/conventions/zfc-compliance.md`.

## Source layout

| Area | Live path |
| --- | --- |
| Adapters and execution protocols | `src/codeprobe/adapters/` |
| Ranking, statistics, and trace analysis | `src/codeprobe/analysis/` |
| Repository assessment | `src/codeprobe/assess/` |
| CLI commands | `src/codeprobe/cli/` |
| Runtime configuration | `src/codeprobe/config/` |
| Execution, isolation, and scoring | `src/codeprobe/core/` |
| Task loading and models | `src/codeprobe/loaders/`, `src/codeprobe/models/` |
| Mining and curation | `src/codeprobe/mining/` |
| Outcome tracking | `src/codeprobe/outcomes/` |
| Snapshot/redaction/export | `src/codeprobe/snapshot/` |
| Tests and policy lints | `tests/`, `tests/lint/` |

The Adapter + Collector hybrid has three extension contracts:

- `AgentAdapter` and `AgentOutput`: `src/codeprobe/adapters/protocol.py`
- `SessionCollector`: `src/codeprobe/adapters/session.py`
- `TelemetryCollector`: `src/codeprobe/adapters/telemetry.py`

Every adapter must preserve partial results with an error field and extract
token/cost telemetry with an accurate `cost_source`. Never drop failed trials or
silently turn unavailable telemetry into measured data.

## Scoring and verifier honesty

The former single scoring module is now a package:

- Public imports: `src/codeprobe/core/scoring/__init__.py`
- `ScoreResult`, `Scorer`, and `SCORER_FAMILIES`:
  `src/codeprobe/core/scoring/result.py`
- Scorer implementations and ground-truth validation:
  `src/codeprobe/core/scoring/scorers.py`
- IR arithmetic: `src/codeprobe/core/scoring/ir.py`
- Workspace materialization: `src/codeprobe/core/scoring/materialize.py`
- Sandbox execution and secret sanitization:
  `src/codeprobe/core/scoring/sandbox.py`

`tests/lint/test_scorer_honesty.py` enforces six rules:

1. Every `ScoreResult(...)` constructor declares `scorer_family=`.
2. F1-family branches do not quietly return recall/weighted recall.
3. Scorer code does not introduce hardcoded semantic thresholds.
4. Scorer code does not use unannotated broad exception handlers.
5. Migrated scorer families declare a typed `verdict=`.
6. Composite scorers parse their stdout contract before a successful process
   exit can award positive reward.

The positive-reward exit fallback rule intentionally targets scorer code only.
Mining-writer verifier names and stub prevention are representability concerns
enforced by writer validation and its focused tests, not reward calculation.

To add a scorer family:

1. Register it in `SCORER_FAMILIES` in
   `src/codeprobe/core/scoring/result.py`.
2. Implement or wire it through `src/codeprobe/core/scoring/scorers.py` or
   `src/codeprobe/core/scoring/ir.py`.
3. Document the reward and `sub_scores` shape in `docs/scoring_model.md`.
4. Add fixture-backed coverage in `tests/test_scoring_reward.py`.
5. Run the scorer-honesty lint below.

Known lint exceptions live in `_KNOWN_OFFENDERS` in
`tests/lint/test_scorer_honesty.py`; each entry needs a reason, line range,
reviewer sign-off, and follow-up bead. Remove entries when their offender is
fixed.

## ZFC boundary

Application code may perform IO, validation, mechanical parsing, deterministic
arithmetic, and structural checks. Semantic judgments such as quality,
difficulty, planning, or meaning belong to models or explicit user input, not
keyword/regex heuristics or hardcoded thresholds.

Before changing orchestration or a tracked heuristic, read and update
`docs/conventions/zfc-compliance.md`. The blocking mechanical lint is
`scripts/lint_zfc.py`; it complements the documented inventory rather than
replacing review.

## Development and verification

Install the project and development tools into the repository environment:

```bash
uv sync --extra dev
```

Run the narrowest relevant test while iterating. Before handing off a code
change, reproduce the CI gates:

```bash
uv run ruff check src/ tests/ scripts/
uv run mypy src/codeprobe --strict-optional
uv run pytest tests/ -x --cov=src/codeprobe --cov-fail-under=80
uv run python3 scripts/lint_zfc.py src/codeprobe/ \
  --allowlist scripts/lint_zfc.allowlist.toml
uv run pytest tests/lint/test_scorer_honesty*.py -q
```

For documentation-only guidance changes, verify every backticked repository
path resolves and run the focused policy checks whose commands changed. Do not
claim the full suite passed unless it was run.

## Bead close evidence

Before closing a standard codeprobe bead, set:

```bash
bd update <bead-id> \
  --set-metadata "evidence.artifact_path=git:<main-reachable-sha>" \
  --set-metadata "evidence.reviewer_verdict=<pass|fail|pass-with-caveats>" \
  --set-metadata "evidence.reviewer_agent=<agent-or-session>"
python3 scripts/check_bead_reachability.py <bead-id>
bd update <bead-id> --status=closed --notes "<summary>"
```

For genuinely documentation-only evidence, use a repository path and set
`evidence.doc_only=true`. Never cite a feature-branch-only SHA as shipped.
Legitimate bypass reasons begin with `duplicate-of`, `superseded-by`,
`abandoned`, `obsolete`, or `won't-fix`; do not use future-tense bypasses.

## Hands-off boundaries

- Do not push, publish, tag, merge, or release without explicit authorization.
- Do not discard, rewrite, or include unrelated working-tree changes.
- Do not recycle or interrupt active workers from stale sensor output alone;
  corroborate with current session and target-bead state. City infrastructure
  owns sensor repair.
- Do not edit content inside managed marker blocks such as
  `<!-- tom-swe:begin -->` / `<!-- tom-swe:end -->`.
- Keep application behavior unchanged in documentation-only work.
