---
name: probe
description: Generate micro-benchmark probes that test an agent's code navigation and comprehension capabilities. Extracts symbols from a repo and creates fast, exact-match tasks (30s each) covering find-function, count-callers, return-type, and module-dependency probes. Triggers on generate probes, micro probes, probe tasks, probe benchmark, navigation probes, comprehension probes.
user-invocable: false
---

# Probe -- Micro-Benchmark Generator

Generate a battery of fast micro-probe tasks that test an agent's ability to navigate and comprehend a codebase. Each probe has a known ground-truth answer and completes in <30 seconds.

Invokes `codeprobe probe` under the hood -- all generation runs through the CLI, not Python imports.

Works with Python (.py) and TypeScript (.ts/.tsx) repositories.

---

## Probe Types

| Template | Category | Question | Answer Type |
|----------|----------|----------|-------------|
| `find_function` | `probe_navigate` | "What file contains the function `X`?" | file_path |
| `count_callers` | `probe_comprehend` | "How many files import/call `X`?" | integer |
| `return_type` | `probe_comprehend` | "What does `Class.method` return?" | text |
| `module_dependency` | `probe_comprehend` | "Does module A depend on module B?" | boolean |

---

## Phase 1: Gather Information

**Question 1** -- Header: "Repository to probe"
- "Which repository should I generate probes for?"
- Options:
  - **Current directory** -- "Use the current working directory"
  - **Specify path** -- "Enter a local path to the repository"

If the user specifies a path, validate it exists and is a git repository.

**Question 2** -- Header: "Probe count"
- "How many probes should I generate?"
- Options:
  - **Quick (10)** -- "Fast smoke test, ~5 minutes to run"
  - **Standard (30)** -- "Good coverage, ~15 minutes to run"
  - **Thorough (50)** -- "Comprehensive, ~25 minutes to run"

**Question 3** -- Header: "Language filter"
- "Which languages should I probe?"
- Options:
  - **All supported** -- "Python + TypeScript"
  - **Python only** -- "Only .py files"
  - **TypeScript only** -- "Only .ts/.tsx files"

---

## Phase 2: Generate Probes

Run the probe generator:

```bash
codeprobe probe "{REPO_PATH}" \
  --count {COUNT} \
  --output "{OUTPUT_DIR}" \
  --seed 42 \
  {--lang python | --lang typescript}
```

Where:
- `{REPO_PATH}` is the validated repository path
- `{COUNT}` is the selected probe count (10, 30, or 50)
- `{OUTPUT_DIR}` is `{REPO_PATH}/probes/` by default, or a user-specified location
- Language flag is omitted for "all supported"

Add `--json` to capture structured output for the summary:

```bash
codeprobe probe "{REPO_PATH}" --count {COUNT} --output "{OUTPUT_DIR}" --seed 42 --json
```

---

## Phase 3: Report Results

Display a summary table:

```
Generated {N} micro-probes for {repo_name}:

| Type              | Count | Difficulty |
|-------------------|-------|------------|
| find_function     | {n1}  | easy       |
| count_callers     | {n2}  | medium     |
| return_type       | {n3}  | medium     |
| module_dependency | {n4}  | easy       |

Output: {OUTPUT_DIR}
```

---

## Phase 4: Run (Optional)

**Question** -- Header: "Run probes now?"
- "Would you like to run these probes against the current agent?"
- Options:
  - **Run now** -- "Execute with /run-eval"
  - **Skip** -- "Just generate, I'll run later"

If "Run now", delegate to `/run-eval` with the output directory.

---

## Task Output Format

Each probe becomes a standard task directory:

```
probe-findfunction-001/
  task.toml          # category="probe_navigate", reward_type="exact_match", time_limit_sec=30
  instruction.md     # The probe question
  tests/
    ground_truth.json  # {"answer": "...", "answer_type": "file_path|integer|boolean|text"}
    test.sh            # Extract agent answer, compare to ground truth
```

### Scoring

Probes use exact-match scoring via `test.sh`:
- **file_path**: Normalize slashes, strip `./`, case-insensitive comparison
- **integer**: Extract first integer from response
- **boolean**: Normalize yes/no/true/false
- **text**: Case-insensitive with whitespace normalization

---

## Integration with Other Skills

- `/run-eval`: Probes are standard tasks -- run them with `/run-eval {output_dir}`
- `/interpret`: Probe results appear as a "probe profile" -- navigation vs comprehension scores
- `/experiment`: Add probe generation as a task source alongside PR mining

---

## Capability Tags

Each probe is tagged for capability analysis:
- `navigation` + `symbol_search`: find_function probes
- `comprehension` + `cross_reference`: count_callers probes
- `comprehension` + `type_analysis`: return_type probes
- `comprehension` + `dependency_analysis`: module_dependency probes

These tags enable radar-chart visualization in `/interpret`.

---

## Quick Reference

| User says | What happens |
|-----------|-------------|
| `/probe` | Full guided flow (Phase 1 -> 2 -> 3 -> 4) |
| "generate probes" | Same as `/probe` |
| "probe this repo" | Uses current directory, asks count/lang |
| "quick probe" | 10 probes, all languages, current directory |
| "probe Python only" | Filters to .py files |
