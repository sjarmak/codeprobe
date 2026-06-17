# Architecture diagram (LikeC4)

Architecture-as-code model of `codeprobe`, rendered with
[LikeC4](https://likec4.dev). The model is the source of truth across
[`spec.c4`](spec.c4) (element kinds, tags, deployment node kinds),
[`model.c4`](model.c4) (the system), and [`views.c4`](views.c4) (structure,
walkthrough, and risk views), with the deployment model in
[`deployment.c4`](deployment.c4). The narrative companion is the repo-root
[`README.md`](../README.md) and the conventions in [`AGENTS.md`](../AGENTS.md).

`codeprobe` benchmarks AI coding agents against *your own* codebase: it mines
real tasks from a repo's git history, replays agents (Claude Code / Copilot /
Codex) against them in isolation, scores the output against an oracle, and ranks
the configurations — the **assess → mine → run → score → interpret** spine.

Every element `link`s to its source (`src/codeprobe/…`) and, where one exists, to
the relevant convention doc (`docs/conventions/…`, `docs/adapters.md`,
`docs/scoring_model.md`) — so any box in the explorer is one click from the code
and the rule behind it.

## Delivery state is tagged, not guessed

Every element carries a tag so **moving / opt-in work renders distinctly from
what is built and exercised** (legend in `spec.c4`):

| Tag | Meaning | Render |
|---|---|---|
| `#built` | code path exists and is exercised (tests + CLI surface) | solid |
| `#evolving` | built, but the contract / coverage is still moving | solid amber |
| `#planned` | designed (PRD exists); not yet implemented (or v1 is a stub) | **dashed, dimmed** |
| `#research` | speculative / opt-in library track, not wired to the CLI | **dashed, indigo** |

The project is `Development Status :: 3 - Alpha` (`pyproject.toml`), so most of
the spine is `#built`. `#evolving` items: the org-scale comprehension families,
the VCS-host abstraction (GitLab implemented, others fall back to git log), the
Jira tracker, the Codex/Copilot adapters, MCP policy/discovery, the local
session-ratings collector, and the opt-in container sandbox. The `contrib/`
advanced-methods library (SPRT, Elo, tournament, Pareto, …) is `#research` —
importable but deliberately off the CLI. No `#planned` placeholder code lives in
the tree; the PRDs under `docs/prd/` describe work that has largely landed.

## Views

**Structure** — the static map:

| View | Scope |
|---|---|
| `index` | system landscape — `codeprobe` in context of git hosts, Sourcegraph, issue trackers, LLM backends, the container engine, and the agent under test |
| `codeprobeSystem` | the `codeprobe` system decomposed into containers (the assess → mine → run → score → interpret spine) |
| `cliContainer` | CLI command group and subcommands |
| `miningContainer` | mining internals — extractor, org-scale families, multi-backend consensus, curator, AST oracle, VCS/tracker |
| `executionContainer` | the execution engine — executor, worktree isolation, adapter registry, MCP policy |
| `adaptersContainer` | the Adapter + Collector boundary (AgentAdapter / SessionCollector / TelemetryCollector + concrete adapters) |
| `scoringContainer` | scorer families, IR reward, scoring sandbox, bias detection |
| `analysisContainer` | deterministic ranking + statistics + report formats |
| `calibrationContainer` | validity gates — R11 calibration, triad fixtures, cross-task QA |
| `snapshotContainer` | redacted snapshots, canary secret gate, SQLite trace recorder |
| `supportContainer` | supporting libraries — domain models, config, LLM registry, preambles, net, ratings |
| `research` | the `contrib` library + opt-in / evolving edges, with built dependencies dimmed |
| `deployment` | where each piece runs — process & trust boundaries (CLI process, agent child process, opt-in container sandbox, external APIs) |

**Walkthrough flows** (dynamic / numbered-step views) — the narrative spine for
a design-review walkthrough:

| View | Flow |
|---|---|
| `mineFlow` | mining tasks from a repo with a multi-backend consensus oracle |
| `runFlow` | replaying one agent config against a task in an isolated worktree |
| `scoreFlow` | scoring agent output against the oracle through an honesty-linted reward family |
| `interpretFlow` | interpreting results with the three bias guards before ranking |

**Risk lens:**

| View | Scope |
|---|---|
| `risks` | the `#risk`-flagged elements with each open question stated in-box — the MCP ground-truth tautology (`codeprobe-ekhi`), the recall-fallback verifier-honesty trap (voxa-class regression), and the suppressed-winner case when no independent baseline exists |

### Running the walkthrough

For a design review, present in this order: `index` → `codeprobeSystem` (orient
on structure) → the four walkthrough flows in sequence (mine → run → score →
interpret, what actually happens) → `deployment` (where it runs) → `risks` (what
to probe) → `research` (what's opt-in / moving). In `npx likec4 start`, the
dynamic views animate step-by-step and each view's notes panel carries the
gotchas (the consensus / `--no-consensus` caveat, the per-run worktree
isolation, the scorer-honesty linting, the suppressed-winner rule).

## Viewing & regenerating

```bash
# Interactive, hot-reloading explorer (recommended)
npx likec4 start architecture

# Re-export the static PNGs in exports/ (needs a one-time browser download:
#   npx playwright install chromium-headless-shell)
npx likec4 export png architecture -o architecture/exports

# Validate the model (strict — the source of truth for correctness)
npx likec4 validate architecture
```

### Viewing the interactive explorer over SSH (headless remote)

`likec4 start` serves a Vite dev server on `localhost:5173`. From a headless
remote, forward that port to your laptop and open it locally — three options,
easiest first:

1. **VS Code / Cursor Remote-SSH** — run `npx likec4 start architecture` in the
   integrated terminal; the editor auto-forwards 5173 and offers "Open in
   Browser". Nothing else to configure.
2. **SSH local port-forward** — on your laptop:
   ```bash
   ssh -N -L 5173:localhost:5173 user@remote   # leave running
   ```
   then on the remote `npx likec4 start architecture` and open
   <http://localhost:5173> locally. (Already in an SSH session? Add the tunnel
   without reconnecting: press `~C` then type `-L 5173:localhost:5173`.)
3. **Bind + reach directly** — `npx likec4 start architecture --listen 0.0.0.0`
   and browse to `http://<remote-ip>:5173` (only if that port is reachable /
   firewall-open; the tunnel in option 2 is safer).

No browser at all? Export the static PNGs with `npx likec4 export png` (needs no
display) — `scp` them down, or view inline if your terminal supports images.
