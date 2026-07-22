---
name: codeprobe-mine
description: Mine eval tasks from a repository's history using the codeprobe CLI. Extracts real code-change tasks from merged PRs/MRs with ground truth, test scripts, and scoring rubrics. Triggers on mine tasks, extract tasks, propose tasks, benchmark my repo, eval my repo, discover tasks. Use this when the agent needs to produce a reusable task suite from a codebase.
user-invocable: false
---

# codeprobe mine (autonomous agent contract)

Mine real eval tasks from a repository's merge history. The resulting task
directories contain instruction.md, test.sh, metadata.json, and the ground-truth
diff required for automated scoring.

## Environment (pre-loaded)

The following commands are executed and their JSON envelopes are read into the
prompt before this skill's body runs. Treat them as authoritative context:

- !`codeprobe doctor --json`
- !`codeprobe check-infra offline --json`

If doctor's envelope reports `status != "ok"`, resolve the flagged checks before
invoking `codeprobe mine`. If check-infra offline reports a TTL shorter than the
expected mining duration, either extend credentials or omit `--offline` at call
time.

## Bare invocation

Minimum viable call. Always pair `--json` with `--no-interactive` for agents —
the default CLI is TTY-interactive:

```bash
codeprobe mine <repo_path> --json --no-interactive --goal general --count 5
```

For MCP/tool-benefit task mining:

```bash
codeprobe mine <repo_path> --json --no-interactive --goal mcp --count 10
```

When the repo has no merged PRs (squash-only history) the default narrative
source is undetectable and mining fails loudly. In that case, pass a commit-
based narrative source explicitly:

```bash
codeprobe mine <repo_path> --json --no-interactive --narrative-source commits
```

## JSON fields to parse

`--json` emits a single terminal envelope on stdout with this shape:

```json
{
  "status": "ok" | "error",
  "command": "mine",
  "exit_code": 0,
  "data": {
    "tasks": [ { "id": "...", "path": "...", "type": "...", "difficulty": "..." } ],
    "count": <int>,
    "output_dir": "<abs-path>"
  },
  "errors": [
    { "code": "<CODE>", "message": "...", "remediation": "...", "terminal": <bool> }
  ]
}
```

Parse `status`; on `"error"` inspect `errors[0].code`. On `"ok"` read
`data.tasks[].path` for the mined task directories.

## Error handling

Only the codes below may surface from this command. Look them up in
`src/codeprobe/cli/error_codes.json` for the authoritative description and
remediation pattern.

| Code | Kind | Retryable? | Action |
|---|---|---|---|
| NARRATIVE_SOURCE_UNDETECTABLE | prescriptive | yes (with fix) | Re-run with explicit `--narrative-source commits` or `--narrative-source commits+rfcs`. |
| GOAL_UNDETECTABLE | diagnostic | yes (with fix) | Pass an explicit `--goal`; re-run. |
| INVALID_GIT_URL | prescriptive | yes (with fix) | Re-issue with a well-formed `<repo_path>` (absolute local dir or valid git URL). |
| CLONE_FAILED | diagnostic | yes (bounded) | Inspect credentials/network; retry once more. Stop after second failure. |
| OFFLINE_PREFLIGHT_FAILED | diagnostic | no | Resolve preflight output from pre-loaded `check-infra offline` envelope; do not retry blindly. |
| METADATA_MISSING | diagnostic | no | Structural problem in the target repo or cached fixture; stop and surface to caller. |
| LLM_UNAVAILABLE | diagnostic | yes (bounded) | Treat as transient provider outage; retry once. |
| INTERRUPTED | diagnostic | **TERMINAL — do not retry** | User/signal halted the run. Preserve partial output; exit. |

## Retry policy

- Maximum retry depth per error chain: **2**. After two consecutive errors
  sharing the same code, stop and surface the envelope to the caller.
- Terminal errors (INTERRUPTED, BUDGET_EXCEEDED) are **never** retried.
- Do not auto-change flags on retry unless the `remediation` field explicitly
  tells you which flag to set (e.g. NARRATIVE_SOURCE_UNDETECTABLE →
  `--narrative-source commits`). Arbitrary flag mutation is out of scope.
- Between retries, re-read the pre-loaded doctor envelope; if doctor now
  reports failing checks, stop.
