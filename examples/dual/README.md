# Dual Verification Example Tasks

This directory contains example tasks demonstrating codeprobe's **dual verification** mode. Dual verification combines two independent scoring signals for the same task:

1. **Direct verification** (`tests/test.sh`) — a runnable test script that checks the agent's actual code change. Returns binary pass/fail.
2. **Artifact verification** (`tests/ground_truth.json`) — a structured `answer` value that the agent's reported result is matched against. This catches "test passes by accident" and "right answer / wrong code" failure modes.

The two signals are combined according to the task's `scoring_policy` (`min`, `mean`, or `weighted`). When they disagree the run is flagged for human review.

## Why dual?

Test scripts can be gamed (hardcoded outputs, masked failures) and ground-truth comparisons can be too rigid (semantically correct but textually different). Together they provide a more robust signal:

- Test passes + answer matches → high confidence pass
- Test passes + answer mismatches → likely test gaming or wrong scope
- Test fails + answer matches → likely incomplete implementation
- Test fails + answer mismatches → clear failure

## Layout

```
examples/dual/
├── README.md                  (this file)
├── comprehension/             (10+ org-scale / comprehension style)
│   ├── count-functions/
│   ├── trace-dependency/
│   └── ...
└── sdlc/                      (10+ SDLC code-change style)
    ├── add-docstring/
    ├── fix-import/
    └── ...
```

Every task directory contains:

- `task.toml` — task metadata, including `[verification]` with `verification_mode = "dual"`, `scoring_policy`, and weights
- `instruction.md` — human-readable task instructions for the agent
- `tests/test.sh` — executable bash script that exits 0 on pass, non-zero on fail
- `tests/ground_truth.json` — JSON object with `answer_type` and `answer` fields

## Running validate

Each example passes `codeprobe validate`:

```bash
uv run codeprobe validate examples/dual/comprehension/count-functions
uv run codeprobe validate examples/dual/sdlc/add-docstring
```

The test suite `tests/test_examples_dual.py` iterates every example and asserts validation succeeds. Run it with:

```bash
pytest tests/test_examples_dual.py -v
```

## Walk-through: comprehension example

`examples/dual/comprehension/count-functions` asks the agent to count the number of `def` declarations in a target file. Its ground truth is:

```json
{
  "answer_type": "integer",
  "answer": 3
}
```

Its `test.sh` independently greps for `def ` and asserts the count matches the expected value. The dual scorer compares both signals.

## Walk-through: SDLC example

`examples/dual/sdlc/add-docstring` asks the agent to add a module-level docstring to a stub Python file. Its ground truth records the expected boolean outcome:

```json
{
  "answer_type": "boolean",
  "answer": true
}
```

Its `test.sh` checks that the file's first AST node is a string literal (i.e. a docstring). The dual scorer combines a structural assertion (test) with the artifact answer.

## Important notes

- **These tasks are synthetic.** They exist to demonstrate the format and seed adoption, NOT to benchmark real agents. Most tests are deliberately trivial (e.g. `exit 0`) so the structure is the focus.
- **Scoring policies vary.** Some examples use `min` (strictest), some `mean` (balanced), some `weighted` (asymmetric). This shows the configuration surface.
- **Languages vary.** Most examples use Python or shell, but the dual format is language-agnostic.

To create your own dual task, copy one of these directories as a starting point and replace the contents.
