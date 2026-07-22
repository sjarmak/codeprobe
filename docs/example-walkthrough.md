# Worked example: a merged PR becomes an evaluation

This walkthrough runs the full CodeProbe loop end to end with real commands and
real output. The mining and validation steps are fully offline and
reproducible. The `run` step needs an installed coding agent and an API key, so
its numbers depend on the agent you point at it; the report *format* shown here
comes straight from `codeprobe.analysis.report.format_text_report`.

## 1. The input: a merged change in repo history

CodeProbe reconstructs tasks from merged pull requests and merge commits. The
example below mines the CodeProbe repository itself. One of its own merged
changes was the "Agent-friendly CLI" work, landed as merge commit `cb4bbd77`,
which touched a large set of CLI and test files.

## 2. Mine one task (offline)

```bash
codeprobe mine . --goal quality --count 1 --no-llm --no-interactive
```

Real output (trimmed):

```
Analyzing up to 8 merge commits...
INFO: Detected source: github (sjarmak/codeprobe)
INFO: Mined 1 tasks from 1 merge commits (min_files=2)

Mined 1 tasks:

   #  Task ID        Difficulty   Language     Quality
  ----------------------------------------------------
   1  cb4bbd77       hard         python          50%

====================================================
Mining summary
====================================================
  Tasks mined:     1
  Quality gate:    1 warning(s)
  Instructions:    regex fallback
  Output:          ./.codeprobe/tasks
  Suite manifest:  ./.codeprobe/suite.toml
====================================================
```

`--no-llm` skips instruction enrichment, so the task's problem statement is the
raw commit body (the "regex fallback" note above). Without `--no-llm`, mining
calls an LLM to rewrite the instructions into a clean problem statement plus
acceptance criteria, with a 60s per-task timeout and an honest fallback to the
template text on timeout or error.

## 3. The generated task

The mined task is a directory under `.codeprobe/tasks/<id>/`. Its
`metadata.json` records the provenance and how the agent's change is verified:

```json
{
  "id": "cb4bbd77",
  "repo": "codeprobe",
  "metadata": {
    "difficulty": "hard",
    "language": "python",
    "task_type": "sdlc_code_change",
    "ground_truth_commit": "cb4bbd77d64b52bc6a5ad1793de56c9276073edb",
    "enrichment_source": "pr"
  },
  "verification": {
    "type": "test_script",
    "command": "pytest tests/cli/test_envelope.py tests/cli/test_errors.py ...",
    "reward_type": "continuous"
  }
}
```

The verification command is the set of test files the original change touched.
The agent works against the repository state *before* the change; the tests
carry the expected outcome. Task information (the instruction) is kept separate
from the expected solution (the recorded ground-truth commit).

## 4. The agent configuration

An experiment pins the full setup being measured: agent, model, and any MCP
tools or preamble. Configs are additive, so you can compare several in one run.

```bash
codeprobe experiment init . --name compare
codeprobe experiment add-config ./compare \
  --label haiku --agent claude --model claude-haiku-4-5-20251001
codeprobe experiment add-config ./compare \
  --label sonnet --agent claude --model claude-sonnet-4-6
```

## 5. Run and interpret

```bash
codeprobe run ./compare --max-cost-usd 5.00
codeprobe interpret ./compare
```

`interpret` ranks the configs and prints a report. The shape below is the exact
format emitted by `format_text_report`; the specific scores depend on the agent
you ran.

```
## Experiment: compare

### Rankings
1. sonnet — 82% pass rate, $1.94 total — recommended
2. haiku — 61% pass rate, $0.38 total — cheaper, lower pass rate

### Per-Task Results

| Config | Task     | Score | Pass | Duration (s) | Cost ($) |
|--------|----------|-------|------|--------------|----------|
| sonnet | cb4bbd77 | 0.82  | Y    | 143.2        | 0.6100   |
| haiku  | cb4bbd77 | 0.40  | N    | 96.7         | 0.0900   |
```

Add `--format html` to write a self-contained `compare_report.html`, or
`--format csv` / `--format json` for pivot tables and machine consumption. Cost
figures carry a `cost_source` (`api_reported`, `calculated`, `estimated`, or
`unavailable`) so estimated numbers are never presented as measured.

## Fully offline: validate the committed example tasks

The `examples/dual/` directory ships committed task directories that validate
without any agent or network:

```bash
codeprobe validate examples/dual/sdlc/fix-import
```

```
  PASS  instruction.md exists (instruction.md present and non-empty)
  PASS  metadata parses (task.toml parsed successfully)
  PASS  task_type valid (task_type 'sdlc_code_change' is valid)
  PASS  verification_mode valid (verification_mode 'dual' is valid)
  PASS  tests/test.sh exists and executable (tests/test.sh present and executable)
  PASS  tests/ground_truth.json valid (ground_truth.json valid with 'answer' field)
  PASS  scoring_policy valid (scoring_policy 'mean' is valid)
```

These tasks are synthetic and exist to demonstrate the task format, not to
benchmark agents. See [`examples/dual/README.md`](../examples/dual/README.md)
for the dual-verification format they illustrate.
