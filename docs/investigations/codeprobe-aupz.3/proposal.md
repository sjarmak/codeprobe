# codeprobe-aupz.3 — Partial-progress checkpoint when the turn budget is nearly exhausted

**Bead:** codeprobe-dg0p (`[aupz-followup.3]`, research/exploratory, read-only)
**Parent:** codeprobe-aupz (`docs/investigations/codeprobe-aupz/eval_writeup.md`)
**Verdict:** **Not pursuing live `turns_remaining` injection — the Claude Code CLI does not support it on codeprobe's execution path. The cap retune (codeprobe-4cl6 / `aupz-followup.1`) is the correct structural fix.** One near-zero-cost complement is noted below.

---

## 1. The problem this bead investigated

The aupz writeup showed **13/15 SDLC trials hit `error_max_turns=50` and scored `reward=0.0`** — the agent was making forward progress (writes/refactors in flight) but had **no signal that termination was imminent**, so it left no scorable artifact behind. This bead asked: can the agent be told "you have ≤5 turns left, save partial progress now" so cap-cutoff trials produce *some* verifier-scorable edit instead of 0.0?

---

## 2. How the cap is enforced today (codeprobe)

The turn cap is a **silent, adapter-side kill-switch**. It is plumbed config → adapter → CLI flag, and the agent is never told it exists:

- `src/codeprobe/templates/evalrc-mcp-comparison.yaml:15` — `max_turns: 50`
- `src/codeprobe/config/loader.py:137-152` — validates and carries `max_turns` onto `AgentConfig`
- `src/codeprobe/adapters/claude.py:355-360` — emits `--max-turns <N>` onto the `claude -p` command line
- `src/codeprobe/adapters/telemetry.py:282-289` — a `max_turns` hit returns as an error envelope (`is_error=true`), with real cost/token data preserved

Nothing surfaces a turn budget *to the agent*: the preambles (`src/codeprobe/preambles/*.md`) contain **no** turns-remaining or save-partial-progress guidance. The agent runs blind to the cap and is killed at turn N.

---

## 3. SDK capability question — answered (with citations)

**Does the Claude Code CLI / Agent SDK surface "turns remaining" to the agent? → No (for turns). The only adjacent mechanism is an alpha *token* budget, not a turn budget, and it is not on codeprobe's execution path.**

- **CLI `--max-turns`** is documented as a pure external stop condition:
  > "Limit the number of agentic turns (print mode only). Exits with an error when the limit is reached. No limit by default."
  — Claude Code CLI reference, `--max-turns` row. <https://code.claude.com/docs/en/cli-reference>

- **Agent SDK `maxTurns`** behaves identically and is explicitly *not* surfaced to the model. Per the TypeScript Agent SDK reference (<https://code.claude.com/docs/en/agent-sdk/typescript>):
  - `maxTurns` = "Maximum agentic turns (tool-use round trips)"; the limit is an external counter that stops the loop — **the model is not told how many turns remain.**
  - The turn count (`num_turns`) is exposed **only after completion** in `SDKResultMessage`. There is **no callback, hook, or message that exposes the current turn number or remaining turns mid-session** — the agent cannot introspect its turn budget while running.

- **Partial exception — alpha `taskBudget`** (same SDK reference):
  > "*Alpha.* API-side task budget in **tokens**. When set, the model is told its remaining token budget so it can pace tool use and wrap up before the limit."

  This is the *only* mechanism that surfaces a budget to the model for self-pacing — exactly the behavior dg0p wants — but it is (a) **token-based, not turn-based** (codeprobe caps on turns, and the aupz failures were turn-cap hits), (b) **alpha** (stability/contract risk), and (c) an **Agent SDK option**, whereas codeprobe executes through the `claude -p` **CLI** (`adapters/claude.py` builds an argv and runs the binary — it does not embed the SDK). Adopting it would mean migrating codeprobe's executor off the CLI onto the Agent SDK *and* switching the cap's semantics from turns to tokens.

---

## 4. Cross-rig comparison — EB and CSB do the same thing

Both sibling rigs treat `--max-turns` as the identical silent kill-switch; **neither surfaces remaining turns to the agent**:

| Rig | Cap | Mechanism | Surfaces remaining turns? |
| --- | --- | --- | --- |
| **codeprobe** | 50 (`evalrc-mcp-comparison.yaml`) | `--max-turns` CLI flag (`adapters/claude.py`) | No |
| **EnterpriseBench** | 50 (hardcoded) | `--max-turns 50` in `DEFAULT_OAUTH_AGENT_COMMAND`, `scripts/orchestration/run_task.py:218` | No |
| **CodeScaleBench** | 30 (default) | `--max-turns {max_turns}`, `scripts/running/daytona_runner.py:725` (documented `docs/DAYTONA.md:246`) | No |

There is no prior art to port — the whole fleet runs the cap blind.

---

## 5. Estimate — would a preamble rule "if turns_remaining ≤ 5, save progress" work?

**No, not as a pure preamble rule.** The rule has nothing to read: `turns_remaining` is never present in the model's context (§3), and the model cannot reliably self-count agentic turns. A "≤ 5 turns left" instruction would be the agent guessing.

To make a *live* countdown work you would need one of:

- **(a) Adapter-side per-turn injection at `cap − 5`** — **not feasible on codeprobe's path.** `claude -p` is a single fire-and-forget subprocess (`adapters/claude.py`); the adapter has no per-turn callback to inject a warning mid-run. This would require moving to the Agent SDK's streaming/hook surface — a large change for a P3 exploratory bead.
- **(b) The alpha `taskBudget`** — token-based, alpha, and SDK-only (§3). Speculative for production eval use.

Both are high-cost / high-risk relative to the value, and (b) changes the cap's meaning.

---

## 6. Decision

**Not pursuing live `turns_remaining` surfacing.** Rationale: the capability does not exist on codeprobe's CLI execution path, the only adjacent mechanism (alpha `taskBudget`) is token-based + alpha + requires an SDK migration, and the simpler structural fix already has a bead.

- **Primary fix (already filed):** **codeprobe-4cl6** (`aupz-followup.1`) — find the smallest turn cap that does not collapse reward. Raising/tuning the cap directly removes the cap-cutoff-at-0.0 failures the aupz writeup measured, with no SDK dependency. codeprobe-gg9f (`aupz-followup.2`, task-category-aware caps) further targets the long-running tasks where the cap is unavoidably tight.

- **Optional near-zero-cost complement (no new heavy bead needed):** add a **static** cap-awareness line to the preambles (`src/codeprobe/preambles/*.md`) — e.g. *"You run under a hard turn limit. Save/commit incremental progress as you go; do not leave work uncommitted while exploring, or a cutoff will discard it."* This needs **no** live `turns_remaining` (it is static text), costs one line, carries no alpha/SDK risk, and addresses the aupz root cause ("the agent had no signal the cap existed") by making the agent checkpoint-by-default. It is strictly better than today's fully-blind kill-switch, though weaker than a true live countdown. Recommend folding this into the codeprobe-4cl6 cap-retune work rather than filing a standalone implementation bead — it is too small to own a bead and belongs with the retune it complements.

- **Watch item (not now):** if codeprobe ever migrates its executor from the `claude -p` CLI to the Agent SDK and adopts token-based budgets, revisit the alpha `taskBudget` option for genuine model-side self-pacing.

---

## 7. Acceptance checklist

- [x] Proposal doc exists at `docs/investigations/codeprobe-aupz.3/proposal.md`.
- [x] SDK capability question answered (turns: no; alpha token `taskBudget`: partial) with citations to the CLI and Agent SDK references.
- [x] Cross-rig comparison summarized (EB cap=50, CSB cap=30 — both silent kill-switches, neither surfaces remaining turns).
- [x] Closed with a "not pursuing — `aupz-followup.1` (codeprobe-4cl6) cap retune is the right fix" verdict; optional static-preamble complement noted, no standalone implementation bead filed.

---

## Sources

- Claude Code CLI reference — `--max-turns`: <https://code.claude.com/docs/en/cli-reference>
- Claude Agent SDK (TypeScript) reference — `maxTurns`, `num_turns`, alpha `taskBudget`: <https://code.claude.com/docs/en/agent-sdk/typescript>
- `docs/investigations/codeprobe-aupz/eval_writeup.md` — cap-hit trials scoring 0.0
- codeprobe: `adapters/claude.py:355-360`, `config/loader.py:137-152`, `templates/evalrc-mcp-comparison.yaml:15`, `adapters/telemetry.py:282-289`
- EnterpriseBench: `scripts/orchestration/run_task.py:218`
- CodeScaleBench: `scripts/running/daytona_runner.py:725`, `docs/DAYTONA.md:246`
