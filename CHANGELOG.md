# Changelog

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
