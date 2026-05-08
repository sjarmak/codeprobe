# codeprobe-jf28 — preamble v2 + /all endpoint + sg-only isolation

## Scope

Bead `codeprobe-jf28` ships three coupled changes that prepare codeprobe to
run sg-only trials with the same isolation guarantees CSB and EB already
provide:

1. **Preamble v2.** Replace `src/codeprobe/preambles/sourcegraph.md` with a
   tighter, decision-table-driven body that emphasises range-bounded reads
   and keyword-vs-NLS query selection.
2. **MCP endpoint switch.** `https://sourcegraph.com/.api/mcp/v1` →
   `https://sourcegraph.com/.api/mcp/all`.
3. **File-removal-and-bring-back isolation.** Port the equivalent of CSB's
   `Dockerfile.sg_only` and EB's `generate_sg_only_dockerfile` into
   codeprobe's executor so an `sg_only` trial really has no local source
   for the agent to fall back on.

## Diff vs the v1 preamble

The v1 preamble (commit `474f6cd…d60ca12`) tried to defend against agents
routing reads through `mcp__sourcegraph__read_file` even when files
existed locally at `repo_path`, by emitting an explicit
"Prefer local-first" guardrail and a "When To Use MCP vs Local Tools"
decision table. The v2 preamble takes the opposite, cleaner approach: pair
the preamble with file-removal isolation so local source genuinely is not
present, and have the preamble state that fact directly:

> **Local source files are not present.** Your workspace does not contain
> source code. You **MUST** use Sourcegraph MCP tools to discover, read,
> and understand code before making any changes.

Other notable shifts:

- Single decision-table for tool selection (vs the v1 prose layout).
- Explicit query-construction rule: extracted keywords, not natural-language
  questions. `sg_nls_search` stems automatically.
- Range-bounded reads are the default (`sg_read_file` with `startLine`/
  `endLine`), not full-file fetches.
- Two template slots — `{{repo_scope}}` (the indexed-repo one-liner) and
  `{{workflow_tail}}` (the per-category continuation of the numbered
  workflow) — replace the v1 preamble's five per-category insertion
  points (`sg_local_search_step`, `sg_negative_result_handling`, etc.).

## Endpoint URL

The bead asked for verification before edit. Two existing references in
the tree already used the `/all` form:

- `tests/test_secret_redaction.py:192` (a "demo.sourcegraph.com" fixture)

So `/all` is the correct, current form. Updated literals:

- `README.md:173` — example `--mcp-config` JSON
- `.claude/skills/experiment/SKILL.md:138, 303` — example configs
- `src/codeprobe/cli/wizard.py:35` — wizard URL builder
- `src/codeprobe/templates/evalrc-mcp-comparison.yaml:20` — shipped template
- `src/codeprobe/mining/sg_ground_truth.py:103` — sg_find_references caller
- Test fixtures: `tests/test_secret_redaction.py`, `tests/test_mcp_policy.py`,
  `tests/test_init_wizard.py`, `tests/test_bias_detection.py`,
  `tests/test_sg_ground_truth.py`

## File-removal pattern source

CSB and EB both implement source removal via Docker:

- **CSB** ships per-task `Dockerfile.sg_only` files (e.g.
  `sourcegraph_benchmarks/ccx-sgauth-301/environment/Dockerfile.sg_only`)
  with an empty `WORKDIR /workspace` and `ENV SOURCEGRAPH_REPOS=…`. The
  harness picks the variant via `_parse_sourcegraph_repos_from_dockerfile`
  in `agents/harnesses/base.py:176`.
- **EB** generates the same shape at runtime via
  `scripts/sandbox/dockerfile_generator.py:219`
  (`generate_sg_only_dockerfile`). It writes a marker file
  `/tmp/.sg_only_mode` so verifiers can detect the mode.

Codeprobe doesn't run agents inside Docker. Worktree-pool isolation is
the equivalent layer where the analogous primitive lives, so the port
landed there as a context manager
(`src/codeprobe/core/isolation.py: quarantine_local_source`):

- All top-level entries of the workspace except `.git`, `.codeprobe`,
  and any `.codeprobe-worktrees*` (worktree pool dirs) are atomically
  moved to a sibling stash directory on enter.
- The agent runs with an empty workspace.
- On exit (including on exception), entries are restored. Files the
  agent created during the yield window — typically `answer.txt` /
  `answer.json` — survive because they didn't conflict with any
  stashed name. If the agent did write a same-named entry, the agent's
  version wins (cleanest for a fresh trial; the source state was
  read-only from codeprobe's perspective).
- A `keep` tuple lets callers preserve task-specific in-tree fixtures
  when needed; the default empty tuple is the common case because
  codeprobe's scoring path reads `tests/test.sh` from `task_dir` (a
  separate path), not from the workspace.

## Wiring

- New `ExperimentConfig.hide_local_source: bool = False` field
  (`src/codeprobe/models/experiment.py`).
- Loaded from YAML evalrc via the explicit-configs path
  (`src/codeprobe/config/loader.py`).
- New CLI flag `codeprobe experiment add-config … --hide-local-source`
  (`src/codeprobe/cli/__init__.py`).
- `execute_task` opts into the context manager when the flag is set,
  threaded through `execute_config`
  (`src/codeprobe/core/executor.py`).

## Test changes

| Test | Change |
|------|--------|
| `tests/test_preamble.py::test_compose_instruction_sourcegraph_symbol_reference_is_authoritative` | "grep union" → "keyword-search union" — local Grep is unavailable under sg-only mode. |
| `tests/test_preamble.py::test_compose_instruction_sourcegraph_default_keeps_broad_recall_guidance` | Removed "supplement with local Grep" assertion; replaced with "Union sg_keyword_search and sg_nls_search" check. |
| `tests/test_preamble.py::test_compose_instruction_sourcegraph_sdlc_forbids_mcp_read_for_local_files` | Renamed to `test_compose_instruction_sourcegraph_declares_no_local_source` — the v1 MCP-vs-local guardrail is replaced by the v2 "Local source files are not present" framing. |
| `tests/test_preamble.py::test_builtin_sourcegraph_preamble_exists` | Pin `{{repo_scope}}` and `{{workflow_tail}}` instead of `{{sg_repo}}`. |
| `tests/test_preamble.py::test_builtin_preamble_renders_variables` | Render with the new slots; assert no template tokens leak through. |
| `tests/test_show_prompt.py::test_show_prompt_sourcegraph_uses_task_specific_guidance` | "grep union" → "keyword-search union". |
| `tests/cli/test_run_cmd_resolved_instruction.py::test_resolved_instruction_renders_task_preamble_context` | Same phrase swap. |
| `tests/cli/test_no_bare_usage_errors.py` | Whitelist line numbers shifted +1/+2 in `experiment_cmd.py` after the new `hide_local_source` parameter. |

## New tests

- `tests/test_isolation.py::TestQuarantineLocalSource` — 10 unit tests
  covering stash/restore, default-keep entries, custom keep, agent
  output preservation, exception safety, no-op cases, and stash-dir
  cleanup.
- `tests/test_executor.py::test_execute_task_hide_local_source_stashes_during_run`
  — integration test wiring the flag end-to-end. A recording adapter
  snapshots workspace contents during `run()` and verifies they were
  empty (apart from `.git`); after restore, source files are intact
  and the agent's `answer.txt` survives.
- `tests/test_executor.py::test_execute_task_hide_local_source_default_false_keeps_source_visible`
  — pin the default behaviour: source is visible without the flag.

## Test results

- **Affected tests:** 330 passing (preamble + isolation + executor + MCP +
  CLI flag whitelist).
- **Full suite:** 3672 passing, 6 failing.
- **All 6 failures preexist on the unmodified branch** (`git stash` →
  same 6 fail): 5 in `tests/mining/test_ast_resolver.py` (Go AST
  parser), 1 in `tests/test_experiment_cmd.py::test_validate_ready`
  (promotion-gate confidence threshold). Out of scope for jf28.

## Smoke trial

Not run in this changeset. Codeprobe authenticates Claude Code via OAuth
(no per-run API billing), so the smoke trial only needs a configured
experiment dir + `SOURCEGRAPH_TOKEN`. A staged experiment exists at
`~/test_repos/gascity/gascity-mcp-comparison/`. The mechanism is
unit-tested and integration-tested; a real-trial smoke is the natural
next step before codeprobe-4cl6 (SDLC cap retune sweep) reruns against
the new preamble + endpoint + isolation.

To run the smoke trial manually:

```bash
codeprobe experiment add-config <exp-dir> \
  --label with-sg-isolated \
  --agent claude --model claude-sonnet-4-6 \
  --preamble sourcegraph \
  --mcp-config '{"mcpServers":{"sourcegraph":{"type":"http","url":"https://sourcegraph.com/.api/mcp/all","headers":{"Authorization":"token ${SOURCEGRAPH_TOKEN}"}}}}' \
  --hide-local-source

codeprobe run <exp-dir> --max-cost-usd 0.50 --task-id <one-task>
```

Acceptance for the smoke (per bead):

- Trial envelope shows the agent invoked MCP tools (not local `Read`).
- Source files restored after the trial; `answer.txt` survives.
- Ground-truth/test files in `task_dir` were untouched (they're outside
  the workspace, so this is structurally guaranteed by codeprobe's
  scoring path — but worth eyeballing the run dir).

## Sequencing

This bead unblocks `codeprobe-4cl6` (SDLC cap retune) and `codeprobe-gg9f`
(per-family caps). Both should rerun against the v2 preamble + `/all`
endpoint + sg-only isolation rather than the v1 surface.
