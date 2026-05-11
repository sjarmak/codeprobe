# codeprobe

Python eval framework for comparing AI coding agents (Claude Code, Copilot, Codex) on quality, cost, and speed.

## Beads (Task Tracking)

This project uses `bd` (beads) for task tracking. Epic: `codeprobe-ssf`.

### MANDATORY: Bead Cold-Start Rule

Every bead description MUST contain enough context that a fresh agent session can execute the work without running explore subagents. This is non-negotiable.

**Required in every bead:**

1. **Exact file paths** — `src/myproject/widgets/widget_service.py`, not "the widget file"
2. **Line numbers or function names** — `line 43` or `parse_output()`
3. **Numbered implementation steps** — what to do, in what order
4. **Code snippets / data shapes** — JSON schemas, Protocol signatures, dataclass fields for anything non-obvious
5. **Reference files with context** — `~/projects/MCP-Eval-Tasks/scripts/run_experiment.py lines 178-194, look for envelope.get('usage')`
6. **Acceptance criteria** — checkboxes so the agent knows when it's done
7. **Test fixture descriptions** — what test files to create and their contents
8. **Dependency context** — what prior beads changed and how it affects this work

**Validation check before closing bead creation:** "Could a fresh agent implement this by reading only the bead description, the referenced files, and the PRD?"

**Research-phase beads** (where the work IS exploration): provide a concrete checklist of commands to run, files/URLs to check, and questions to answer. Never open-ended "investigate this area."

### MANDATORY: Bead Close Ritual

The city's close-gate-reaper enforces evidence metadata on codeprobe bead closes. Beads whose title starts with `[` (the standard codeprobe convention, e.g. `[r1/codeprobe-evjr]`, `[3oms-followup]`, `[infra]`) are scanned by rule `codeprobe-drain-without-commit-guard`. **A close that doesn't set the three required fields will be reopened by the reaper within an hour.**

Before running `bd update --status=closed` (or `bd close`), set ALL THREE of these metadata fields:

```bash
bd update <bead-id> \
  --set-metadata "evidence.artifact_path=git:<commit-sha>" \
  --set-metadata "evidence.reviewer_verdict=<pass|fail|pass-with-caveats>" \
  --set-metadata "evidence.reviewer_agent=<your-session-or-agent-name>"

bd update <bead-id> --status=closed --notes "<brief summary>"
```

Field semantics:

- **`evidence.artifact_path`** — commit hash (`git:<sha>`), writeup path, or test file. Multiple comma-separated values are fine: `git:abc123,docs/investigations/x/writeup.md`. **Critical:** any `git:<sha>` reference MUST be reachable from `main` (i.e. the commit is merged), not a feature-branch-only commit.
- **`evidence.reviewer_verdict`** — short freeform string. `pass`, `fail`, `pass-with-caveats` are typical; longer cycle markers like `9th-cycle-verified` are also accepted.
- **`evidence.reviewer_agent`** — who is signing off. Typically your own session ID or agent name.

**Verify reachability before close:**

```bash
# Confirm the commit is on main (or your default branch). Exit 0 = merged.
git merge-base --is-ancestor <commit-sha> main && echo "OK: merged" || echo "NOT MERGED — do not close yet"
```

A commit on a feature branch is NOT shipped. Closing a bead with `evidence.artifact_path=git:<sha>` where the sha is feature-branch-only puts the bead store and git out of sync — the bead says shipped, git says the work is unreachable from `main`. This pattern was found in zelda and in codeprobe's own evjr.* beads (May 2026): all 6 evjr commits closed as shipped while still living only on `feature/codeprobe-x7p3-validate-unified-contract`. Don't add to this debt.

**Why the metadata even matters:** the reaper has no view into git. It only checks bead metadata. If you make a commit but don't set `evidence.artifact_path`, the reaper sees an empty field and reopens the close. This is the surface-level cause of the evjr.* 13-cycle reopen loops in May 2026 — workers shipped real commits but never wrote the metadata, so each close was rolled back automatically. The deeper cause (commits not reaching `main`) is what the reachability requirement above addresses.

**Bypass for legitimate exception cases** (e.g. duplicate-of, superseded-by): set `metadata.gate_bypass="<reason>"` instead of evidence fields. The reaper respects bypass and won't reopen.

**Tracking:** the upstream proposal `gascity gc-n5j` (close-time policy hook) would let `bd close` reject the close at source with a clear error instead of relying on post-hoc reopen. Until that lands, the workaround is this ritual.

## Architecture

See `prd_agent_adapter_architecture.md` for the full PRD with converge debate results.

Key architecture: Adapter + Collector hybrid

- `AgentAdapter` Protocol (headless): `name`, `preflight()`, `run()` → `AgentOutput`
- `SessionCollector` Protocol (interactive): `start_capture()`, `snapshot()`, `stop_capture()` → `AgentOutput`
- `TelemetryCollector` Protocol (shared): token/cost extraction, composed into both

## ZFC Compliance

This project is AI-orchestration code — ZFC applies at two levels:

1. **L2 (tooling):** codeprobe's own orchestration code must not use heuristics for semantic judgment
2. **L3 (product):** defaults and heuristics embedded in codeprobe shape how users perceive their benchmarks

### Compliant

- `core/scoring.py` — delegates pass/fail to test.sh (gold standard ZFC); IR scorers report reward = recall (or `weighted_recall` for tiered oracles) with precision/F1 in `ir_metrics`. The split is documented arithmetic, not judgment — see `docs/scoring_model.md`
- `core/llm.py` — shared Claude CLI utility for model-based judgment (pure IO + mechanical parsing)
- `analysis/ranking.py` — deterministic arithmetic with explicit tiebreakers
- `analysis/trace_quality.py` — mechanical projection of `CompletedTask` + `BiasWarning` records onto a per-trial quality view; sole threshold (`LOW_RECALL_THRESHOLD`) is a documented constant that surfaces an existing oracle metric, not a quality verdict (see `docs/trace_quality.md`)
- `adapters/` — mechanical parsing, honest about data quality via `cost_source`
- `analysis/stats.py` — arithmetic aggregation (deterministic math, not judgment)
- `assess/heuristics.py:score_repo_with_model()` — delegates scoring to Claude via fixed RUBRIC_V1; model judges quality, code does IO
- `mining/extractor.py:generate_instruction()` — delegates instruction.md generation to LLM; regex fallback only for `--no-llm` offline mode
- `config/defaults.py` narrative-source resolver — delegates selection to `core/llm.py` under the fixed rubric `_NARRATIVE_RUBRIC_V1`; falls back to the deterministic priority `pr > commits > rfcs > issues` only when no LLM backend is available or `offline=True`, and emits an `LLM_UNAVAILABLE` envelope warning so callers see the degraded mode (PRD §13-T4 refactor)

### Known violations (tracked for refactoring)

- `mining/extractor.py:80-87` — file-count difficulty estimation (≤3 → easy, >10 → hard). A 20-file rename is "hard" while a critical 1-file security fix is "easy". Replace with model-assessed difficulty or user-provided metadata
- `assess/heuristics.py:_detect_test_frameworks()` — regex framework detection. Structural file-glob part is OK, but "does this repo have good test coverage?" is semantic — delegate to model
- `cli/mine_cmd.py:_quality_review()` — three heuristics: length+keyword check for "thin instructions" (desc < 50 chars), hardcoded 0.7 threshold for "low diversity", stub command keyword match. These are UI warnings, not scoring judgments, so lower priority for refactoring
- `mining/org_scale_families.py` — `min_hits` thresholds (3-5) are hardcoded. Structural file-counting is OK per ZFC, but the thresholds are arbitrary. Acceptable as tunable parameters
- `mining/curator_tiers.py:assign_ground_truth_tiers()` — the `use_llm=False` branch (line ~410) returns the pure mechanical heuristic tiers without any LLM call. This is a documented offline fallback mode; callers that opt in accept the ZFC trade-off. Not a drift bug — refactor would instead tighten the docstring/labeling so consumers know when they're seeing heuristic-only tiers

### Justified exceptions

- `analysis/stats.py` — arithmetic aggregation is deterministic math, not judgment
- Secret redaction regex in `scoring.py` — pattern matching for known token formats is structural, not semantic
- `core/isolation.py:_collect_scaffold_paths` / `_collect_overlay_files` (codeprobe-2nw2 scaffold mode) — TRUNCATE_EXTENSIONS allowlist + path-prefix excludes (`.git/`, `tests/`, `.codeprobe/`, `.claude/`, `.github/workflows/`) for sg-only scaffolding. Pure structural file-system metadata comparison (suffix membership, size > 0, prefix match against a manifest captured at context-manager entry) with no semantic judgment about file content. See `docs/investigations/codeprobe-2nw2/design.md` §ZFC compliance note.

### When to update this section

Update ZFC compliance notes when: new heuristic code is introduced, a known violation is refactored to use model calls, or a new justified exception is added. Not per-commit — only when the heuristic landscape changes.

## Verifier-honesty lint (`tests/lint/test_scorer_honesty.py`)

A pytest-based lint over `core/scoring.py` and `core/bias_detection.py` that catches four classes of *verifier dishonesty*:

1. **`missing-scorer-family`** — every `ScoreResult(...)` constructor must declare which rubric produced the reward. Empty strings are accepted (the field is documented as "opaque" in that case); the kwarg must be present.
2. **`quiet-recall-fallback`** — F1-family branches (`oracle_overlap_f1`, `oracle_overlap_fbeta`, `oracle_weighted_f1`) that fall back to `reward = recall` / `reward = weighted_recall`. The voxa-class regression: caller asked for a precision-sensitive reward and got recall.
3. **`hardcoded-threshold`** — both inline float literals in compares (`if x < 0.7`) and module-level threshold-named constants (`_FOO_THRESHOLD = 0.5`). Named constants are honest documentation but still not config-plumbed.
4. **`bare-except`** — bare `except:` and `except Exception:` without `# noqa` annotation in scorer code.

### Adding a new scorer family

1. Register the family name in `SCORER_FAMILIES` (`core/scoring.py`).
2. Document the rubric (sub_scores shape) in `docs/scoring_model.md`.
3. Every `ScoreResult` your scorer emits must declare `scorer_family=` (the lint enforces this).
4. Add a fixture-backed test in `tests/test_scoring_reward.py`.

### Allowing a known violation

Pre-existing offenders are tracked in `_KNOWN_OFFENDERS` in `tests/lint/test_scorer_honesty.py`. Each entry pins the file path, line range, rule code, reason, and a follow-up bead ID. Adding a new entry needs reviewer sign-off; the lint is a CI gate, not a suggestion. When an offender is fixed, delete the matching entry — the `test_scorer_honesty_known_offenders_still_present` test flags stale allowlist entries so cleanup is enforced.

## Release Process

1. Bump `version` in `pyproject.toml`
2. Commit: `chore: bump version to X.Y.Z`
3. Tag: `git tag vX.Y.Z`
4. Push commit and tag: `git push && git push --tags`
5. GitHub Actions (`.github/workflows/publish.yml`) runs tests on 3.11/3.12/3.13, then publishes to PyPI via twine using the `CODEPROBE` secret

## Key Constraints

- ALL adapters must extract token/cost data — never just document a shortcoming
- Validate-or-die on all data boundaries (premortem finding)
- Partial results preserved with error field, never crash silently
- Score failures as "incorrect" rather than dropping them
