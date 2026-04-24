# Changelog

## 0.7.1 (2026-04-24)

Point release burning down post-0.7.0 follow-up beads. Highlights:

### Features

- **Tenant collision lock (bead `codeprobe-gq1`, PRD R4).** `mine`, `run`, and `snapshot create` now hold an advisory `fcntl.flock`-based lock at `~/.codeprobe/state/{tenant}/.lock-{command}` for the duration of the invocation. A second invocation in the same tenant + command raises a new `TENANT_IN_USE` diagnostic error naming the live holder PID instead of silently racing on state. Stale PIDs are reclaimed transparently. `CODEPROBE_DISABLE_TENANT_LOCK=1` is an emergency escape hatch; Windows falls back to a no-op with a warning.
- **User-home skill migration helper (bead `codeprobe-coa`, PRD §13-T5).** New `codeprobe skills migrate` subcommand rewrites the pre-v0.6 user-home skills at `~/.claude/skills/{mine-tasks, run-eval, interpret, check-infra, calibrate}/` as `DEPRECATED:` stubs pointing at the authoritative `.claude/skills/codeprobe-*/SKILL.md`. Idempotent. TTY prompts unless `--yes` is passed; non-TTY refuses without `CODEPROBE_SKILLS_MIGRATE=ack`. `codeprobe doctor` grew a `user-home skills up to date` check that emits the existing `STALE_USER_HOME_SKILL` diagnostic.

### ZFC debt paid

- **Narrative-source selection now delegates to `core/llm.py`** (bead `codeprobe-0vk`, PRD §13-T4). `config/defaults.py:resolve_narrative_source` prompts an LLM under the fixed rubric `_NARRATIVE_RUBRIC_V1` and falls back to the deterministic priority `pr > commits > rfcs > issues` only when `offline=True` or no LLM backend is available — in which case the caller emits an `LLM_UNAVAILABLE` envelope warning. The SLO entry previously tracked in `CLAUDE.md § Known violations` is removed; the self-enforcing guard in `tests/zfc/test_narrative_source_slo.py` now flips to a regression check against re-introduction. Closes before the 2026-10-23 deadline.

### CI / tooling

- **mypy `--strict-optional` passes cleanly** (bead `codeprobe-0ms`). The blocking `ci.yml` gate stops sitting red for the first time since v0.5.5: burned 109 pre-existing errors across 40+ files. Added `scipy-stubs` and `types-requests` to `[dev]` extras.
- **GitHub Actions bumps** (bead `codeprobe-cyh`). `checkout@v4→v5`, `setup-python@v5→v6` across `ci.yml`, `ci-latest.yml`, and `publish.yml` ahead of the 2026-09-16 Node 20 runner cut-off.

### Deprecations

- **Exception classes renamed to carry the PEP 8 / ruff N818 `Error` suffix** (bead `codeprobe-6c9`). Each old name is preserved as a module-level alias via `__getattr__` that emits `DeprecationWarning` on access; the aliases will be removed in **v0.9**. Migration: update your imports to the new name.

  | Old | New |
  |-----|-----|
  | `CalibrationRejected` | `CalibrationRejectedError` |
  | `RetryLimitExceeded` | `RetryLimitExceededError` |
  | `AuthFailure` | `AuthFailureError` |
  | `SandboxWriteDenied` | `SandboxWriteDeniedError` |
  | `CanaryFailed` | `CanaryFailedError` |
  | `CanaryProofInvalid` | `CanaryProofInvalidError` |
  | `ScannerUnavailable` | `ScannerUnavailableError` |
  | `TraceBudgetExceeded` | `TraceBudgetExceededError` |

  The `N818` ignore has been removed from `pyproject.toml [tool.ruff.lint]`.

### New envelope error codes

- `TENANT_IN_USE` — terminal diagnostic raised by the new tenant lock.
- `LLM_UNAVAILABLE` — warning emitted when the narrative-source resolver falls back to the deterministic priority because no LLM backend is available.

## 0.6.0 (2026-04-23)

Large release landing the "Enterprise Repo Benchmark Parity" PRD (25 units across 5 layers, 27 commits). Net: +~744 tests (2096 → 2840 passing). Architecture supports enterprise constraints (non-GitHub VCS, non-GitHub trackers, self-hosted LLMs, on-prem MCP, airgapped runs) while OSS-repo consistency remains the near-term priority.

### Features — mining

- **Structured `answer.json` oracle (`oracle_type="structured_retrieval"`).** New schema `{files, symbols, chain, text}`, per-field F1 scoring, fail-loud on malformed/missing (no `$AGENT_OUTPUT` fallback). Legacy `answer_type="file_list"` preserved. Tests: `tests/mining/test_oracle_structured_retrieval.py`.
- **Widened MCP instruction trigger.** `instruction_mcp.md` now emitted for `task_type in {"mcp_tool_usage", "org_scale_cross_repo"}`, `org_scale=True`, or non-empty `sg_repo`. Body rendered from capability map, not hardcoded tool names.
- **Curator-assigned `oracle_tiers`.** Mining ground-truth files are tiered `required` / `supplementary` / `context` via `curator_backends.invoke_model` (ZFC-compliant). Hardcoded `tier="required"` assignments removed from `org_scale.py`.
- **New `dependency_upgrade` task type.** Mines PRs touching only dependency manifests + lockfiles with a semver-bump title. Model-classified (ZFC-compliant). Covers package.json/pnpm-lock, go.mod/go.sum, pyproject/poetry.lock, Cargo, Gemfile.
- **AST-ranked cross-repo discovery.** `mining/multi_repo.py::discover_related_repos` parses go.mod / package.json (npm/yarn/pnpm) / pyproject.toml candidates and ranks by real AST reference hits into candidate exports. Manifest declarations with zero AST hits are rejected (not ranked "low").
- **Pluggable `NarrativeAdapter` interface.** Three shipped adapters: PR/MR, plain commits, RFC docs. Explicit selection via `--narrative-source` (INV1 — no silent fallback). Mining against a squash-only repo fails loudly with a prescriptive error.
- **GitLab VCS + Jira tracker adapters** with auth-hygiene contract (`redact_request`/`redact_response` applied before any log/event). Zero-tolerance token-leak CI gate. OAuth 2.0 + PAT.
- **Resumable tenant-scoped mining state** at `~/.codeprobe/state/{tenant_id}/{repo_hash}/mine.db`. WAL + `synchronous=FULL` + `BEGIN IMMEDIATE`. Startup sweep promotes stale `running`/`pending` rows to `interrupted`. Repo-level `flock()` around worktree create/remove. `git clone --filter=blob:none` for large repos.
- **`mine --refresh <task-dir>`.** Re-mines against a new commit preserving task IDs where structural identity holds. Jaccard >20% oracle file churn OR oracle_type change fails loud with diff report unless `--accept-structural-change`. Ground-truth commit history tracked on `TaskMetadata.ground_truth_commit_history`.

### Features — runtime

- **SQLite trace store** at `runs/trace.db` with write-side budget enforcement (per-task 10MB / per-run 500MB). Fail-loud overflow by default; `--trace-overflow=truncate` opt-in. Content policy applied before INSERT: env-var value scan, auth-header regex (Authorization/X-Api-Key/AWS session/GCP bearer), `--trace-deny` globs. `codeprobe trace export` produces JSONL.
- **Containerized tool execution** via `src/codeprobe/sandbox/`. `Write`/`Bash`/`Edit` run inside a read-only-bind-mounted container by default (`Dockerfile.sg_only` — python + git + ripgrep + coreutils). `--allow-mutating-tools` for host writes.
- **`instruction.resolved.md`** persisted per task so A/B diffs between configs are auditable from disk.
- **Claude adapter Write+MCP regression fixed.** `--tools ""` no longer strips built-ins when MCP tools are configured. Fixture-replay MCP server in tests.

### Features — backends & capabilities

- **LLM model registry** at `src/codeprobe/llm/model_registry.yaml` mapping logical names (opus-4.7, sonnet-4.6, haiku-4.5) to per-backend IDs across Anthropic, Bedrock, Vertex, Azure OpenAI, and generic OpenAI-compatible. Adapter shims in `src/codeprobe/llm/backends/`. Cross-backend parity fixture test.
- **MCP capability registry** (`src/codeprobe/mcp/capabilities.py`) + Jinja-based preamble templates. Capability-backed `github` + `custom` preambles replace hardcoded tool tables.
- **`check-infra` diagnostics.** `drift` and `preamble-drift` subcommands compare mine-time capability snapshots against live; `offline` subcommand validates reachability and credential TTL across configured backends.
- **Tenant-scoped state paths** (`src/codeprobe/paths.py`). `--tenant` flag; cross-tenant reads fail closed; `codeprobe cache purge --tenant <id>`.

### Features — publishing & analysis

- **`codeprobe snapshot create`** emits CSB-compatible layout: `SNAPSHOT.json` manifest + `summary/{rewards,aggregate,timing,costs}.json` + `traces/` + `export/traces/{config}/{task_id}/`. Relative symlinks rooted in the snapshot; symlink-escape rejected at preflight.
- **Hash manifest + signed attestation** (HMAC-SHA256 with `CODEPROBE_SIGNING_KEY`; unsigned mode documented). Single-byte tampering detected on verify.
- **Redaction capability matrix.** `hashes-only` is the new default for publishable exports (was `none`). `contents`/`secrets` require `--allow-source-in-export`. Canary gate: `secrets` mode refuses to run until the configured scanner demonstrably catches a planted canary. Deterministic scanners only (gitleaks / trufflehog / pattern) — no LLM classification.
- **Dependency-surface snapshot** in `SNAPSHOT.json`: MCP tool schemas, LLM model IDs per backend, issue-tracker API versions, build-manifest parser versions.
- **Observability exporters.** `snapshot export --format {datadog,sigma,sheets,browse}`. `browse.html` is self-contained (inlined CSS/JS, no CDN) for airgapped viewing.
- **Per-checkpoint partial credit** on multi-step tasks. Mining emits `checks/<step>.sh`; `CheckpointScorer` produces `checkpoint_scores` dict in `scoring.json`; `interpret` surfaces per-checkpoint breakdown.
- **Tool-benefit fields on `TaskMetadata`** (`expected_tool_benefit`, `tool_benefit_rationale`, `mcp_capabilities_at_mine_time`). Populated via model call at mine time. `interpret` shows `tool_delta_vs_expected`.
- **Calibration validity gate.** `src/codeprobe/calibration/` with `CalibrationProfile` + `validate_calibration_correlation` (raises below 0.6 Pearson). `codeprobe calibrate` refuses to emit a profile when holdout <100 tasks or <3 repos. (Partner data acquisition deferred.)
- **`interpret --regression`** plots per-task score over time using `ground_truth_commit_history`.

### Infrastructure

- **ZFC boundary lint** (`scripts/lint_zfc.py`). AST-based check rejecting hardcoded semantic-string assignments to `TaskMetadata`-shaped attributes unless preceded by a model invocation in scope. Allowlist at `scripts/lint_zfc.allowlist.toml` for known pre-existing violations.
- **GitHub Actions CI** (`.github/workflows/ci.yml`) — ruff + mypy + pytest with `--cov-fail-under=70` + ZFC lint + snapshot-format compatibility check; matrix 3.11/3.12/3.13.
- **Process docs.** `CONTRIBUTING.md` (second-reviewer + WIP-limit sections), `docs/onboarding/architecture_tour.md`, `docs/discovery/` templates (README, TEMPLATE, INTERVIEW_GUIDE), `docs/CALIBRATION.md`, `docs/SNAPSHOT_REDACTION.md`.

### Known follow-ups (not shipping blockers)

- `src/codeprobe/sandbox/runner.py` RO_WRITE_STDERR_PATTERNS includes `"permission denied"` which can misclassify non-ro-mount permission errors as sandbox denials. Narrow the pattern.
- `instruction.resolved.md` diverges from the real agent prompt in worktree-parallel mode (multi-config) and when preambles reference `{{sg_repo}}`. Two scenarios not covered by tests.
- `tests/trace/` uses unregistered `pytest.mark.unit` / `pytest.mark.integration`. Register in `[tool.pytest.ini_options].markers` to clear warnings.
- Partner-gated acceptance from the PRD (R1 staff-engineer ratings, R3-new type representativeness, R11 ≥100 hand-labeled calibration tasks) is scaffolded but not validated — tracked for later customer rollout.
- PRD Open Questions Q1–Q5 (answer.json schema shape, checkpoint verifier style, snapshot format lock-in, capability versioning, tenant_id semantics) not resolved in-band; Process Precondition P5 sign-off deferred.

## 0.5.5 (2026-04-22)

### Fixes

- **Preamble resolver now wired into `codeprobe run`.** `ExperimentConfig.preambles` has been a field for releases, and `--show-prompt` rendered them correctly, but the actual `codeprobe run` path never constructed a `DefaultPreambleResolver` and `execute_config` received `preamble_resolver=None`. As a result, `load_experiment` silently dropped preambles before v0.5.4 (because they were excluded from the dataclass-from-dict mapping), and once 0.5.4's round-trip fix started preserving them, every run with a non-empty `preambles` hit `RuntimeError: preambles=(...) requested but no preamble_resolver provided`. Now wires up a layered resolver (task-local → project → user → built-in) matching the `--show-prompt` code path.

  This is a real behavior change: experiments that declare `preambles: ["sourcegraph"]` or similar now actually compose the preamble into the prompt sent to the agent. On the kubernetes-mcp-comparison task set, that moved `with-mcp` from 0 true MCP calls to 20+ MCP calls per task.

## 0.5.4 (2026-04-22)

### Features

- **`allowed_tools` / `disallowed_tools` on ExperimentConfig + AgentConfig + `codeprobe experiment add-config`.** Whitelist/blacklist the tools the agent may call, per config. Pass `--allowed-tools ""` (empty string) to disable all built-ins for a true MCP-only comparison; pass a comma-separated list (e.g. `--allowed-tools "mcp__sourcegraph__keyword_search,mcp__sourcegraph__find_references,Write"`) to restrict-and-auto-approve. The adapter emits both `--tools ""` and `--allowedTools <list>` when a whitelist is provided, because in claude 2.1.x `--allowedTools` alone doesn't restrict the tool set — it's the auto-approval list. Verified end-to-end on a kubernetes reference-trace task: a whitelisted MCP-only config produced 15 MCP calls and zero built-in calls, vs. 14/15 built-in calls in the unconstrained baseline.
- **Per-tool usage capture in `CompletedTask.tool_use_by_name`.** Previously `tool_call_count` was always `None` in stored results because the claude adapter used `--output-format json`, which returns `{result, usage, total_cost_usd}` with no message stream. The adapter now uses `--output-format stream-json --verbose` and parses the newline-delimited events to count tool uses (including `mcp__<server>__<tool>` names) while reconstructing the terminal `result` event for downstream code. `JsonStdoutCollector` auto-detects stream-json vs single envelope and handles both, so any other adapter still using the simple envelope keeps working.

### Fixes

- **`ExperimentConfig.__repr__` now redacts and reports `allowed_tools`/`disallowed_tools`** for completeness.

## 0.5.3 (2026-04-22)

### Fixes

- **Pairwise verdict wording no longer overclaims on noise.** `interpret`'s per-pair summary previously said `→ <config> wins` whenever one config had a numerically higher mean score, even when the gap was statistically indistinguishable from zero. It now softens to `→ <config> nominally ahead (not significant; small effect)` when Cohen's d is below 0.2 (or Cliff's delta below 0.147) and/or the Wilcoxon/McNemar p-value is above 0.05, and reports `→ effectively tied` when the score gap is below 0.01. Unqualified `<config> wins` is reserved for cases with a real effect size AND statistical power. Thresholds follow Cohen 1988 (d < 0.2 = negligible) and Romano et al. 2006 (|δ| < 0.147 = negligible).

## 0.5.2 (2026-04-22)

### Fixes

- **`interpret` stats are now score-type-aware.** `codeprobe interpret` and `codeprobe experiment aggregate` previously collapsed continuous F1-style scores to binary pass/fail before computing confidence intervals and effect size. The resulting report declared "100% pass rate" and `effect_size=0.0 cliffs_delta` even when per-task scores ranged 0.08–0.75, hiding the real signal. Three concrete bugs fixed:
  - `analysis/report.py` pre-binarized scores before calling `compare_configs`, so the `_is_binary()` gate always routed into the McNemar + Cliff's delta branch even for continuous scorers. Now passes raw scores; `_is_binary()` correctly selects Wilcoxon + Cohen's d when any score isn't 0 or 1.
  - `analysis/stats.compute_config_summary` / `summarize_completed_tasks` computed `ci_lower/upper` via `wilson_ci(passed, total)` regardless of scorer type. For continuous scorers, CIs are now normal-approximation intervals on the sample mean (`mean_score_ci`), clamped to [0, 1].
  - `ConfigSummary` gains a `score_type: "binary" | "continuous"` field; text rankings show `mean=X.XX [CI a–b]` for continuous and `X% pass rate` for binary.
- Verified on a real N=5 experiment: effect size went from `0.0` (cliffs_delta, broken) to `0.076` (cohens_d, correct); p-value from `null` to `0.25` (Wilcoxon, honest signal for small N); per-config CIs became distinct instead of identical.

## 0.5.1 (2026-04-22)

### Fixes

- **CI dev extras** — add `build>=1.0` to `[project.optional-dependencies].dev`. `tests/test_release_gate.py::test_build_and_stage_real_wheel` shells out to `python -m build --wheel` and was failing the publish-workflow test matrix with `No module named build`. Latent bug since the test was introduced in 3d2cb48 after 0.4.1; v0.5.0 was the first release to exercise it, so publish skipped (no PyPI upload happened).

## 0.5.0 (2026-04-22) — yanked; never published

v0.5.0 failed its publish workflow due to the missing `build` dep above and was never uploaded to PyPI. All v0.5.0 changes ship unchanged in 0.5.1; see that entry for the full list.

### Features

- **`--sg-discovery` flag** — when mining with `--mcp-families`, rank candidate symbols via Sourcegraph `sg_find_references` MCP calls instead of the local grep-based Phase 2 scan. Bounded sample (default 100) + parallel MCP calls cut wall-clock from hours to minutes on large repos (kubernetes: 128min → 22s, ~340× faster). Gated on explicit `--sg-discovery` for backward compat.
- **Tier-weighted F1 by default** — `oracle_check()` now uses `metric="auto"`, which selects `weighted_f1` when `ground_truth.json` has an `oracle_tiers` map and plain `f1` otherwise. Matches CodeScaleBench's `_get_primary_score` behavior. The shipped `tests/oracle.py` template (vendored per task) also reads `oracle_tiers` and produces weighted F1 as the primary reward.
- **Repo-prefix 2-pass path matching** — oracle scoring now strips `<repo>/` (bare or embedded in absolute paths) from agent answers before set comparison, so `kubernetes/pkg/foo.go` and `/home/u/kubernetes/pkg/foo.go` both match oracle `pkg/foo.go`. Requires a new `repo` field in `ground_truth.json` (auto-populated by mining; absent on older tasks falls back to pass-1 matching).

### Fixes

- **Multi-env-var Sourcegraph auth** — `sg_auth.get_valid_token()` now accepts `SRC_ACCESS_TOKEN`, `SOURCEGRAPH_TOKEN`, or `SOURCEGRAPH_ACCESS_TOKEN` (canonical first, aliases for convenience). `SourcegraphBackend` uses the same unified resolver.
- **Fail-loud on missing SG auth with `--mcp-families`** — previously codeprobe silently fell back to grep-only ground truth, producing biased results for the exact MCP-vs-baseline comparison the flag implies. Missing auth is now a hard error with a message listing all accepted env vars, raised before the expensive scan begins.
- **Env-var templates survive MCP config redaction** — `redact_mcp_headers` now preserves values containing `${VAR}` (e.g., `"token ${SG_TOKEN}"`) while still redacting literal secrets. Fixes round-tripping of experiment.json configs that reference secrets via env-var substitution.
- **`CLAUDE_CONFIG_DIR` respected in Claude adapter** — `check_parallel_auth` and `isolate_session` now honor the `CLAUDE_CONFIG_DIR` env var (Claude Code's own account-switching convention) instead of always reading `~/.claude`. Previously missed credentials on systems running Claude Code with an account-specific config dir.
- **Detect expired OAuth tokens in pre-flight** — `check_parallel_auth` now parses `claudeAiOauth.expiresAt` from the credentials file and emits a distinct "credentials EXPIRED" warning with a `claude login` prompt, instead of reporting OK and letting every agent run hit API 401 minutes later.
- **`--sg-repo` help text** — corrected from the misleading `SOURCEGRAPH_TOKEN` reference to list all accepted env var names.

### Behavior notes

- **Scoring change may affect numeric results.** Tasks whose `ground_truth.json` has `oracle_tiers` with mixed tiers (required + supplementary/context) will score differently under the new auto-selected weighted F1. Tasks with all-required tiers are unaffected (weighted F1 ≡ plain F1). Pass `--metric f1` to `codeprobe oracle-check` to force the prior behavior.
- **Mining tasks without `--sg-discovery` still use the grep-based ranking** — the new flag is opt-in. Existing profiles and pipelines keep working.

## 0.3.7 (2026-04-09)

### Features

- **Partial score display** — scores between 0 and 1 show numeric values instead of misleading FAIL; summary shows mean + perfect/partial breakdown
- **Init wizard cached auth** — checks `~/.codeprobe/auth.json` and `SRC_ACCESS_TOKEN` before prompting for Sourcegraph token; offers `codeprobe auth sourcegraph` as recommended path

### Fixes

- **Test path validation** — mined task verification commands now validate that Go package dirs and Python test files exist in the target repo; missing paths are dropped to prevent 0-score failures against stripped repos
- **Removal task verification** — code-deletion PRs (e.g., "remove legacy etcd build") generate `test ! -d` checks instead of trying to `go test` deleted code
- **Skip redundant enrich** — `Next steps` output no longer recommends `--enrich` when LLM already generated instructions

## 0.3.6 (2026-04-09)

### Features

- **Tool-call count tracking** — claude adapter now parses `tool_use` content blocks and propagates `tool_call_count` through `AgentOutput` → `CompletedTask` → `results.json` for tool efficiency analysis
- **Secret redaction** — new `config/redact.py` unconditionally redacts all Authorization header values in `ExperimentConfig.__repr__()` and `experiment.json` serialization

### Fixes

- **Timeout telemetry recovery** — timed-out agent sessions now extract partial token/cost data from stdout instead of discarding all telemetry
- **MCP instruction template** — `mine --goal mcp` now embeds the actual symbol name and definition file into `instruction.md` instead of generic phrasing
- **Test detection heuristic** — broadened to recursive `**/test*/` glob patterns, fixing false negatives for repos with nested test layouts (e.g. numpy)
- **Partial score display** — scores between 0 and 1 now show their numeric value instead of misleading FAIL; summary shows mean + perfect/partial breakdown

### Refactoring

- Batch all test detection globs into a single `git ls-files` call (was up to 22 sequential subprocess calls)
- Surface `parse_output` exceptions in timeout error field instead of silently swallowing
- Derive recursive test file globs from base list to eliminate copy-paste

## 0.3.1 (2026-04-09)

### Fixes

- Remove unsupported `aider` and `openai` agent adapters from registry, entry points, and init wizard — supported agents are now `claude`, `codex`, and `copilot`

## 0.3.0 (2026-04-09)

### Features

- **Layered config resolution** — `--model`, `--timeout`, `--repeats` CLI flags override experiment.json values; precedence logged at debug level
- **`codeprobe doctor`** — environment readiness checker for agents, API keys, git status, Python version with PASS/FAIL and fix suggestions
- **`codeprobe preambles list`** — shows available preambles at built-in/user/project levels with template variables
- **`codeprobe run --show-prompt`** — prints the fully-resolved prompt without spawning an agent (debugging aid)
- **User-defined mine profiles** — `--save-profile`, `--profile`, `--list-profiles` for saving and loading custom flag combinations
- **Mine presets** — `--preset quick` (count=3) and `--preset mcp` (org-scale + MCP families + enrich)
- **Adapter lazy imports** — missing CLI tools no longer crash at import time; clear error at resolve time
- **Adapter output contract tests** — 25 fixture-based tests asserting all adapters report cost/token fields

### Observability (v0.3 backfill)

- **Typed event protocol** — `core/events.py` with 5 frozen dataclass events, queue-based EventDispatcher
- **Rich Live dashboard** — progress, pass rate, cost, ETA during `codeprobe run` (TTY auto-detected)
- **JSON event lines** — `--log-format json` emits structured events on stderr for CI
- **Cost budget warnings** — 80% and 100% thresholds visible on stderr without `-v`
- **Scorer entry_points** — `codeprobe.scorers` group in pyproject.toml; built-in scorers registered through the same mechanism as adapters
- **MCP config discovery** — shared between `init` and `experiment add-config`

### Fixes

- Kill dead `.evalrc.yaml` — removed write from init, deprecation warning when file exists
- Ctrl+C integration test — verifies SIGINT produces exit 130 with no traceback

## 0.1.7 (2026-04-05)

### Features

- Task discovery scoped to current experiment — `mine` records task IDs in `experiment.json`, `run` filters by them
- Backward compatible: old experiments without `task_ids` keep existing behavior (no filtering)

### Fixes

- Fix `run` picking up stale tasks from previous mining runs when multiple task sets coexist

## 0.1.6 (2026-04-05)

### Fixes

- Fix `__version__` out of sync with `pyproject.toml` — CLI now reports correct version
- Skip curation verification when `--no-llm` flag is set

## 0.1.5 (2026-04-04)

### Fixes

- `codeprobe run` now finds tasks at `<repo>/.codeprobe/tasks/` when they're not inside the experiment subdirectory — fixes "No tasks found" after mining

## 0.1.4 (2026-04-04)

### Features

- `codeprobe run` auto-discovers experiments inside `.codeprobe/` — no longer requires `--config` flag when there's exactly one experiment
- Shows helpful disambiguation when multiple experiments exist

## 0.1.3 (2026-04-04)

### Fixes

- Strip markdown fences from LLM JSON responses in regular task mining (extractor.py) — the previous fix in 0.1.0 only covered the org-scale path

## 0.1.2 (2026-04-04)

### Fixes

- MCP config picker now lists all server names instead of truncating with "+N more"

## 0.1.1 (2026-04-04)

### Features

- **Auto-discover MCP configs** — `codeprobe init` now scans known locations (`~/.claude/.mcp.json`, `~/.claude/mcp-configs/`, `settings.json`) and presents a numbered picker with server names instead of requiring a manual path

### Fixes

- Tilde expansion (`~`) now works in `--mcp-config` CLI flag and init wizard path prompts

## 0.1.0 (2026-04-04)

Major release adding org-scale task mining, ground-truth curation, and eval runner improvements.

### Features

- **Org-scale task mining** — mine tasks across organizational codebases with oracle verification and multi-hop dependency tracing (`codeprobe mine --org-scale`)
- **Ground-truth curation pipeline** — curate mined tasks with pluggable backends (grep, agent_search, pr_diff), tier classification (required/supplementary/context), and weighted F1 scoring (`--curate`, `--backends`, `--verify-curation`)
- **LLM tier classification** — Haiku-powered semantic tier assignment for curated files, with heuristic fallback via `--no-llm`
- **Curation verification** — LLM-based sampling to confirm curated file sets are correct (`--verify-curation`)
- **Weighted F1 scoring** — `--metric weighted_f1` in `oracle-check` weights supplementary files lower than required files
- **Multi-repo support** — scan across multiple repositories with `--repos` flag
- **New task families** — cross-repo-config-trace, platform-knowledge, migration-inventory added to org-scale mining
- **Count and boolean oracle types** — beyond file-list oracles, tasks can now use count or boolean answer verification
- **MCP delta validation** — validate MCP tool deltas against ground truth
- **Curation quality reporting** — CLI results table shows curation stats per family
- **Interactive mine workflow** — LLM instruction generation and URL support for mine sources
- **Eval sandbox mode** — eval runs default to `dangerously-skip-permissions` with sandbox signal
- **Instruction discovery variants** — family-specific instruction templates instead of generic placeholders

### Fixes

- Skip curation verification when `--no-llm` flag is set
- Reduce PRDiffBackend noise — shorten window to 3 months, cap at 200 files
- Score partial results from timed-out agents instead of dropping them
- Copy answer.txt from repo to task dir before scoring
- Normalize CLI model names; auto-detect reward_type from task metadata
- Exclude vendor/node_modules/testdata from scanner and merge layer
- Strip markdown fences from LLM JSON responses in task generation
- Filter Python stdlib from dep-trace, cap ground truth at 500 files
- Fix org-scale multi-hop ground truth explosion and dep-trace quality
- PRDiffBackend now checks content_patterns, not just globs

### Refactoring

- Split org_scale.py from 1142 to 462 lines; extract long functions into modules
- Unify `_guess_language` into `mining/_lang.py`
- Remove dead code, improve scanner efficiency, deduplicate logic

## 0.1.0a2 (2026-04-02)

Initial public alpha with core eval pipeline.

## 0.1.0a1 (2026-04-01)

First alpha release.
