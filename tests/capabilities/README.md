# Capability Test Matrix (`tests/capabilities/`)

End-to-end regression net for every advertised codeprobe CLI capability. Complements
the reviewer-driven grading loop (which catches *per-feature quality drift*) by
catching *cross-feature regressions* — silent breakage of capability X caused by a
change to capability Y.

## What this matrix is (and is not)

**Is:**
- Structural regression tests: exit code, required output fields, file
  presence, schema shape.
- Wired into the default pytest collection via `[tool.pytest.ini_options]
  testpaths = ["tests"]`. No extra flags needed — plain `pytest` picks it up.
- Real pipeline code on real fixtures: the actual click commands run through
  `CliRunner`, and the executor/writer/validate paths run for real.

**Is not:**
- A replacement for reviewer-driven grading. If the output is well-formed
  but low-quality, that is what the reviewer gate catches.
- A correctness check for the capability *behavior* — only that it doesn't
  crash, produces the expected shape, and interacts sanely with neighbors.
- A place to fix broken capabilities. If a capability is broken, open a
  linked follow-up bead and keep this matrix narrow.

## Matrix dimensions (aggregate)

| Dimension   | Cells                                                 |
| ----------- | ----------------------------------------------------- |
| Languages   | python, go                                            |
| Task types  | sdlc_code_change, compliance-audit, anchor-fix, dependency_management, architecture_comprehension |
| Oracles     | MCP-Eval-Tasks, EnterpriseBench, synthetic (CliRunner-scoped) |
| Capabilities| validate, assess, mine, run, e2e (mine → validate → run → interpret) |

See `fixtures.py` for the canonical fixture registry.

## Running the matrix

```bash
# Whole matrix (plain pytest picks it up via testpaths)
pytest tests/capabilities/

# Just one capability
pytest tests/capabilities/test_run_capability.py

# Only matrix-parametrized cells (excludes guard/unit-style tests)
pytest tests/capabilities/ -m matrix

# Show per-test durations (per-test budget: ~30s)
pytest tests/capabilities/ --durations=0
```

## What the matrix asserts (per AC)

- **Real CLI**: Every test either invokes `codeprobe <subcommand>` via `click.testing.CliRunner`, or calls the underlying `run_*` / `execute_config` functions. No mocked subprocess wrappers around the pipeline.
- **Real fixtures**: Tests point at real oracle directories under `/home/ds/projects/{MCP-Eval-Tasks,EnterpriseBench,CodeScaleBench}/`, not copies. Missing fixtures skip with a structured reason, they do not silently pass.
- **Structural assertions**: Exit codes, required fields, list lengths, schema presence. No string-exact comparisons on generated content.
- **External-boundary mocks only**: `FakeAdapter` stands in for the agent binary (expensive, stateful). `claude_available` is patched to `False` in the assess tests so scoring runs the deterministic heuristic path. The pipeline logic itself always runs for real.
- **Actionable failures**: Every assertion message names the capability, the fixture, and the concrete mismatch (exit code, list, output snippet). CI/log readers can triage without re-running.

## Adding a new capability test

1. Inspect `src/codeprobe/cli/<your_cmd>.py` for the advertised click command.
2. Add a module at `tests/capabilities/test_<capability>_capability.py`.
3. Decorate the module with `pytestmark = [pytest.mark.capability]`.
4. Write ≥2 tests covering matrix cells (language × task type × oracle), parametrizing over entries from `fixtures.py` where possible.
5. Prefer `CliRunner` for CLI shape tests and the public `run_*`/`execute_*` entry points for programmatic paths. Gate external-service calls with `monkeypatch` at the LLM/network boundary only.
6. Keep each test under ~30 seconds. If a real fixture makes a test slow, pick a smaller fixture or force a deterministic path (`--no-llm`, heuristic fallback).
7. Use the structured assertion helpers from `conftest.py` (`assert_has_fields`, `assert_non_empty_string`) and the "actionable failure" message pattern:

```python
assert result.exit_code == 0, (
    f"capability=<name> fixture={oracle.corpus}/{oracle.name} "
    f"exit_code={result.exit_code} stderr={result.stderr!r} stdout={result.output!r}"
)
```

## Adding a new oracle corpus

1. Drop the corpus path into `fixtures.py` as a module-level constant, e.g.
   `NEW_CORPUS_ROOT = Path("/home/ds/projects/NewCorpus")`.
2. Register one or more fast `OracleFixture` entries pointing at representative tasks (prefer tasks that run in <30s).
3. Add the new fixtures to `ALL_FIXTURES`, `FULL_TASK_FIXTURES`, or `CROSS_MATRIX` as appropriate.
4. In the capability tests, parametrize over the new grouping or add a dedicated test that skips via `require_oracle(fixture)` when the corpus is absent.
5. Update the dimensions table above and `context.md` if the new corpus adds a language or task type to the matrix.

## Known deferrals

- LLM-backed mining (`codeprobe mine` without `--no-llm`) is outside this matrix — too expensive, too flaky. Reviewer-driven grading (br7.1–br7.8) covers generative quality.
- Interactive paths (`codeprobe init` wizard, `codeprobe mine` TTY prompts) are not exercised; the non-interactive flags (`--no-interactive`, `experiment init --non-interactive`) are.
- Real external services (GitHub, Sourcegraph, MCP endpoints) are never contacted. Those flows should be covered by their own integration suites with appropriate secrets and quarantine rules.
