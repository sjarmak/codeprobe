# Agent Configuration

Reference for tunable parameters on `AgentConfig` (the runtime config every
adapter receives) and the matching `ExperimentConfig` fields in
`experiment.json`.

For the full adapter authoring guide see [adapters.md](adapters.md). For
scoring semantics see [scoring_model.md](scoring_model.md).

## `max_turns`

Hard cap on the number of agent turns per task, forwarded to the underlying
agent CLI. Codeprobe's claude adapter passes it through as `--max-turns N`.

| Value         | Behavior                                                  |
| ------------- | --------------------------------------------------------- |
| `None`        | Uncapped. Only the per-task subprocess timeout bounds the run. This is the historical codeprobe default for backwards compatibility. |
| Positive int  | Hard limit; the adapter must reject zero / negative values. |

**This is a hard limit, not a soft cap.** When the agent exceeds it, the CLI
terminates the run — there is no graceful "wrap up" turn. Pick a value
generous enough that healthy runs finish but small enough that runaway loops
get cut off well before the per-task timeout (default 3600 s).

### Reference rigs

The codeprobe-evjr cross-rig audit
([cross_rig_mcp_cost_audit.md](investigations/codeprobe-evjr/cross_rig_mcp_cost_audit.md))
found that the absence of a turn cap was the single largest structural cost
driver vs CSB and EB on MCP-vs-baseline runs:

- **CSB** (`scripts/running/daytona_runner.py`): `CLAUDE_MAX_TURNS = 30`.
- **EB** (`scripts/orchestration/run_task.py`): `--max-turns 50`.
- **codeprobe** (pre-codeprobe-evjr.1): no cap.

For new MCP-comparison experiments, set `max_turns: 50` on every config so
the cap doesn't silently bias one arm against the other.

### Setting it

Three layers, with later layers overriding earlier ones:

1. `experiment.json` config block (cleanest):

   ```json
   {
     "configs": [
       { "label": "baseline",     "max_turns": 50 },
       { "label": "with-mcp",     "max_turns": 50, "mcp_config": { "...": "..." } }
     ]
   }
   ```

2. Legacy `extra` dict — supported for configs authored before the field
   existed:

   ```json
   { "label": "baseline", "extra": { "max_turns": 50 } }
   ```

3. CLI flag (overrides both):

   ```bash
   codeprobe run --max-turns 50 ./my-experiment
   # or
   CODEPROBE_MAX_TURNS=50 codeprobe run ./my-experiment
   ```

### Diagnosing a hit cap

When the cap fires, the claude CLI exits non-zero with a message indicating
the turn limit was reached. The run is recorded with the corresponding error
category so it doesn't get silently dropped — see
[scoring_model.md](scoring_model.md) for how non-pass outcomes are scored.
