# Cold-Adoption Assessment — New-User Journey Gap Audit

**Bead:** codeprobe-fvfo · **Workflow:** codeprobe-aptn9 (mol-focus-review)
**Date:** 2026-06-19 · **codeprobe version:** 0.5.5
**Assessment only — nothing fixed. Numbers below are throwaway (publication hold); the run "scores" are non-run error artifacts, not a comparison result.**

## Persona & method

Role-clamped as a **brand-new codeprobe user with no prior context**: *"a developer with a large Go enterprise codebase (kubernetes / grafana) who wants to A/B test which combination of (model × prompt-variation × available-tools) produces the best coding results on my repo."* Confirmed GENERIC cold start (Stephanie, Slack C0B1A0CKEH0).

Journey walked using **only the public surface** — `README.md`, installed `.claude/skills/codeprobe-*`, and CLI `--help`. **No source under `src/` was read to operate the tool** (A4). The single `src/` exposure encountered was an *unhandled traceback the tool itself printed* (Gap 3), which is logged as a finding, not used as a usage crutch.

Budget: well under the ~$8 cap. The only real agent run cost **$0.00** (it errored before invoking a model — see Gap 1). A large k8s/grafana clone was **not** performed (would blow the wall-clock cap); the large-repo friction is logged analytically from the command surface (Gap 5).

### A1 — journey stages reached

| Stage | Reached? | Outcome |
|-------|----------|---------|
| 1. Install / `--help` / `doctor` | ✅ | CLI works; doctor over-strict (Gap 11) |
| 2. Mine | ✅ | 1 task mined from local repo; multiple defects (Gaps 5–8) |
| 3. Configure A/B arms | ✅ | `init` wizard built a 2-model experiment; single-axis only (Gaps 2, 4) |
| 4. Run → Interpret | ✅ (reached interpretable output) | **Both arms errored on an invalid model token, yet the journey produced a confident "Use sonnet-4" recommendation** (Gaps 1–3) |

All four stages were completed end-to-end. The journey *technically* reached a recommendation — which is precisely the problem: the recommendation is an artifact of silent errors (Gap 1).

### A3 — GUESSED-FLAG scorecard (zero-flag-defaults bar)

**2 GUESSED-FLAG instances directly encountered; 1 latent for the persona's real repo.**

| # | Where | Value I had to supply | Should have | Result |
|---|-------|----------------------|-------------|--------|
| G1 | `init` wizard "Models to compare" | `opus-4,sonnet-4` (guessed from the wizard's own "Sonnet vs Opus" hint + README) | enumerate valid tokens / validate | **invalid — broke the whole run** |
| G2 | `interpret` experiment path | `.codeprobe/fvfo-models` | auto-discover like `run` does | needed only because `interpret .` crashed |
| G3 (latent) | `mine --subsystem` for k8s/grafana | `--subsystem pkg/ …` to avoid whole-monorepo scan | be surfaced in standard help | not encountered (no k8s clone) but unavoidable for the persona |

---

## Ranked gap list (highest leverage first)

### Gap 1 — Errored / non-executed runs are rendered as 0-score rows and a confident recommendation **[CRITICAL]**
- **Stage:** run + interpret · **Type:** BAD-ERROR
- **Evidence:**
  - Run summary (terminal): `cb4bbd77: FAIL (1.9s)` … `Finished: 1/1 tasks, mean score 0.00, total cost $0.00` … `opus-4: 0/1 passed` (same for sonnet-4).
  - Underlying `results.json`: `"status": "error"`, `"error_category": "agent"`, `"cost_source": "unavailable"`, `"input_tokens": 0`, error text *"There's an issue with the selected model (opus-4). It may not exist…"*.
  - `interpret` output: `1. sonnet-4 — 0% pass rate … 2. opus-4 — 0% pass rate` / `opus-4 vs sonnet-4: +0% score, 0.4s slower → effectively tied` / **`Recommendation: Use sonnet-4 for best results.`**
- **Why top:** The persona's entire goal is an actionable comparison. Two arms that **never executed** are presented as a real 0–0 tie and a definitive "use sonnet-4." A cold dev would act on a recommendation backed by zero agent invocations. The 1.5–1.9 s / $0.00 signature is the only clue, and it is not surfaced.
- **Proposed fix:** Exclude `status:"error"` / `cost_source:"unavailable"` runs from rankings; render them as `ERRORED (n)` and refuse a recommendation when no arm produced a real execution.

### Gap 2 — Model tokens are unvalidated, undiscoverable, and the README ships an invalid one **[CRITICAL]**
- **Stage:** configure → run · **Type:** GUESSED-FLAG + DOC-GAP
- **Evidence:** `init` prompt `Models to compare (comma-separated):` has no default, no list, no validation — empty input loops then `Aborted!`. It accepted `opus-4,sonnet-4` unverified. `README.md:263` literally shows `codeprobe run . --model opus-4` (**invalid**), while `README.md:167/207` use valid `claude-haiku-4-5-20251001` / `claude-sonnet-4-6`, and `mine`'s own next-step prints `--model claude-sonnet-4-6`. No `codeprobe models list` affordance exists.
- **Proposed fix:** Validate model tokens at wizard/run entry with a prescriptive error listing accepted tokens; add `codeprobe models list`; correct the `opus-4` example in `README.md:263`.

### Gap 3 — `codeprobe interpret .` (the tool's own suggested next step) crashes with a raw traceback **[CRITICAL]**
- **Stage:** interpret · **Type:** BAD-ERROR + DOC-GAP
- **Evidence:** `run` prints `Next: codeprobe interpret .`; running it raises an unhandled `FileNotFoundError: Experiment not found: …/experiment.json` with a full Python stack trace exposing `src/codeprobe/...` paths (exit 1). `run` auto-discovers the experiment at `.codeprobe/fvfo-models/`; `interpret` does not. Working form `codeprobe interpret .codeprobe/fvfo-models` had to be guessed.
- **Proposed fix:** Make `interpret` auto-discover the experiment like `run`, or emit a prescriptive error ("no experiment.json here — try `codeprobe interpret .codeprobe/<name>`"); never leak a traceback to users.

### Gap 4 — The guided wizard is single-axis; the persona's factorial (model × prompt × tools) is not a first-class flow **[HIGH]**
- **Stage:** configure · **Type:** DOC-GAP / STALL
- **Evidence:** `codeprobe init` offers mutually-exclusive goals — `1. baseline vs MCP` / `2. different models` / `3. different prompts` / `4. Custom`. Choosing "models" configured *only* model arms; there is no guided path to vary models AND prompts AND tools together. The persona's explicit 3-dimension A/B requires hand-assembling arms via `experiment add-config`.
- **Proposed fix:** Add a wizard path (or documented `add-config` recipe) for combined/factorial comparisons.

### Gap 5 — Large-enterprise-repo scoping is buried under `--advanced`; no shallow/URL path **[HIGH — persona's stated repos are k8s/grafana]**
- **Stage:** mine · **Type:** DOC-GAP / STALL (DEAD-END for shallow clones)
- **Evidence:** Standard `mine --help` shows no subset/scope option; the scoping flags `--subsystem pkg/ --subsystem cmd/` and `--discover-subsystems` exist **only** under `codeprobe mine --help --advanced` (46 options) and are absent from the README large-repo guidance. `mine` takes a local PATH and needs merge history, so a `git clone --depth 1` cannot be mined; there is no `mine <url>` for the primary path (only `--cross-repo` accepts a URL). A cold dev must full-clone kubernetes/grafana and mine across the entire history.
- **Proposed fix:** Surface `--subsystem`/`--discover-subsystems` in standard `mine --help` and add a README "mining a large monorepo" callout documenting the full-clone requirement.

### Gap 6 — `--count 3` silently returned 1 task with no reason **[MEDIUM]**
- **Stage:** mine · **Type:** BAD-ERROR (silent shortfall)
- **Evidence:** `codeprobe mine . --goal quality --count 3 --no-interactive` → `Mined 1 tasks` with no explanation of the 3→1 shortfall.
- **Proposed fix:** Report `requested 3, mined 1 (N candidates filtered by quality gate) — lower --min-quality or raise --count`.

### Gap 7 — Status claims "LLM-enriched" after the LLM call timed out and fell back **[MEDIUM]**
- **Stage:** mine · **Type:** BAD-ERROR (status misreports degraded result)
- **Evidence:** `WARNING: LLM instruction generation failed for cb4bbd77: Claude CLI timed out after 60s` → falls back to MCP variant, yet the summary prints `Instructions: LLM-enriched`. (Also: default `mine` calls the LLM even without `--enrich`, adding a 60 s+ per-task timeout window.)
- **Proposed fix:** Status line must reflect fallback ("1 task fell back to template after LLM timeout"); document that default mine performs LLM enrichment.

### Gap 8 — "No difficulty variance" warning gives no actionable remedy **[MEDIUM]**
- **Stage:** mine · **Type:** STALL
- **Evidence:** `! No difficulty variance: all tasks are 'hard'. Need a mix to differentiate models/prompts.` — correct diagnosis, but no next flag to obtain a mix (with only 1 task mined, the A/B is undifferentiable from the start).
- **Proposed fix:** Append concrete remedy flags (e.g., raise `--count`, vary `--min-files`, lower `--min-quality`).

### Gap 9 — README headline does not route the A/B persona to `init` / `experiment` **[MEDIUM]**
- **Stage:** discover · **Type:** DOC-GAP
- **Evidence:** README Quick Start headlines `assess → mine → run → interpret`. The comparison entry point — `init` ("what do you want to learn?") — is the right door for this persona but appears only in the command table; the experiment flow lives in a later "MCP Comparison" section framed around tools, not the general "compare X" goal.
- **Proposed fix:** Add an "I want to compare models/prompts/tools → `codeprobe init`" line to the headline Quick Start.

### Gap 10 — `init`-created `experiment/tasks/` stays empty; `mine` writes to `.codeprobe/tasks/` **[LOW–MEDIUM]**
- **Stage:** configure/mine · **Type:** DOC-GAP
- **Evidence:** Wizard created `.codeprobe/fvfo-models/tasks/` (remained empty); `mine` wrote to `.codeprobe/tasks/`. `run` auto-discovered and reconciled both, but the empty experiment `tasks/` dir is a confusing artifact and the wizard's "Next steps: codeprobe mine ." never mentions the relationship.
- **Proposed fix:** Have the wizard's mine step target the experiment, or document that mined tasks live in the shared `.codeprobe/tasks/`.

### Gap 11 — `doctor` hard-FAILs (exit 2) on missing API keys even when the agent CLI is present and usable **[LOW]**
- **Stage:** install · **Type:** BAD-ERROR (over-strict)
- **Evidence:** `doctor` → `FAIL ANTHROPIC_API_KEY (not set)` / `FAIL OPENAI_API_KEY (not set)` with exit 2, despite `PASS claude CLI (found)`. The later run reached the model layer via the claude CLI without `ANTHROPIC_API_KEY` (it failed on the token, not on auth) — so the key is not actually required for a subscription-auth claude user, making the FAIL a false blocker.
- **Proposed fix:** Demote the API-key check to WARN (not exit-2 FAIL) when the corresponding agent CLI is present and authenticated.

### Gap 12 — `run --help` agent list omits codex **[LOW]**
- **Stage:** run · **Type:** DOC-GAP
- **Evidence:** `run --help` says `--agent ... claude, copilot`, but `doctor`, the `init` prompt, and README prerequisites all include codex.
- **Proposed fix:** Align the `--agent` help string with the supported set.

---

## Verified NON-findings (excluded to avoid misattribution)

- **`doctor` printing JSON:** only when stdout is non-TTY (piped/redirected); on a real TTY it prints a `PASS/FAIL` checklist. Legitimate auto-detection, `CODEPROBE_JSON` was unset — **not** a cold-user defect.
- **`--no-interactive` / `--goal` on mine:** supplied for headless determinism; both have working defaults under a TTY — not counted as GUESSED-FLAG.

## Constraints honored
- Assessment only — **nothing fixed**; each gap should become a follow-up bead (PL + mayor triage).
- No comparison numbers published — the 0.00 "scores" are non-run error artifacts shown solely as evidence for Gap 1.
- A4 — no `src/` read to operate the tool; the one `src/` exposure (Gap 3 traceback) was tool-emitted and is itself a finding.
