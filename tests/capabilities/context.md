# br7.9 context — Full capability test matrix

Deliverable for bead `codeprobe-l6u` ("br7.9 — Full capability test matrix").

## Capabilities covered

| Capability | Module                              | Tests | Uses oracle fixtures              |
| ---------- | ----------------------------------- | ----- | --------------------------------- |
| validate   | `test_validate_capability.py`       | 6     | MCP-Eval-Tasks (ccx-sgauth-301, sg-deepsearch-anchor-fix-001) |
| assess     | `test_assess_capability.py`         | 3     | EnterpriseBench repo + synthetic  |
| mine       | `test_mine_capability.py`           | 4     | synthetic git repo (seeded PR)    |
| run        | `test_run_capability.py`            | 3     | synthetic task dirs + FakeAdapter |
| e2e        | `test_e2e_capability.py`            | 3     | MCP-Eval-Tasks + synthetic        |

**Total: 19 tests.** All modules declare `pytestmark = [pytest.mark.capability]`
and most parametrized cells also carry `@pytest.mark.matrix`. Collected by the
default `pytest` run via `[tool.pytest.ini_options] testpaths = ["tests"]`
(no extra flags).

## Matrix dimensions achieved (aggregate across the suite)

| Dimension   | Cells                                                                                            |
| ----------- | ------------------------------------------------------------------------------------------------ |
| Languages   | **python, go** (≥ 2) ✓                                                                            |
| Task types  | **sdlc_code_change, compliance-audit, anchor-fix, architecture_comprehension (dual mode)** (≥ 2) ✓ |
| Oracles     | **MCP-Eval-Tasks, EnterpriseBench, synthetic** (≥ 2) ✓                                            |
| Capabilities| **mine, assess, validate, run, e2e** (all AC-required capabilities) ✓                             |

EnterpriseBench-only fixtures (`EXAMPLE_TASK.toml`, `dep-mgmt-urllib3-requests-001.toml`)
are registered in `fixtures.py` for future expansion (TOML-oracle shape tests)
but intentionally not parametrized yet — the current validate/run/assess surfaces
work on directory-shaped tasks, not single-file oracle TOMLs.

## Clean baseline pytest output

```
============================= test session starts ==============================
collected 19 items

tests/capabilities/test_assess_capability.py ...                         [ 15%]
tests/capabilities/test_e2e_capability.py ...                            [ 31%]
tests/capabilities/test_mine_capability.py ....                          [ 52%]
tests/capabilities/test_run_capability.py ...                            [ 68%]
tests/capabilities/test_validate_capability.py ......                    [100%]

============================== slowest durations ===============================
0.54s call     tests/capabilities/test_assess_capability.py::test_assess_scores_a_python_repo
0.03s call     tests/capabilities/test_mine_capability.py::test_mine_sdlc_on_synthetic_python_repo
0.02s call     tests/capabilities/test_assess_capability.py::test_assess_on_real_oracle_repo
0.02s call     tests/capabilities/test_e2e_capability.py::test_e2e_validate_to_run_to_interpret_on_synthetic_python

============================== 19 passed in 0.78s ==============================
```

Slowest test: 0.54s. Well under the ~30s/test budget from the bead.

## AC checklist

- [x] `tests/capabilities/` directory exists and is collected by the default runner — verified via `pytest --collect-only -q` picking up 19 tests without extra flags.
- [x] Per-capability modules for **mine, assess, validate, run, e2e** — each with ≥ 2 tests.
- [x] Matrix achieves ≥ 2 languages × ≥ 2 task types × ≥ 2 oracle corpora in aggregate.
- [x] No mocks for the core pipeline — tests use `CliRunner` for CLI shape and the public `execute_config`/`run_validate` for programmatic paths.
- [x] Real oracle fixtures referenced by path under `/home/ds/projects/{MCP-Eval-Tasks,EnterpriseBench,CodeScaleBench}/`; missing fixtures skip with structured reason.
- [x] Assertions are structural (exit code, field presence, type checks, non-empty content) — no string-exact comparisons of generated output.
- [x] Actionable failure output — every assertion message names `capability=`, fixture path/name, and the actual vs expected shape.
- [x] `tests/capabilities/README.md` explains matrix, how to add capabilities, how to add oracles.
- [x] Clean baseline green on `main`.

## Capabilities intentionally deferred

- **LLM-backed `mine`** — Driven by `--no-llm` in tests. Generative mining quality is covered by br7.1–br7.8 reviewer-driven grading.
- **Interactive wizards** (`codeprobe init`, TTY prompts in `mine`) — Only the non-interactive flags are exercised.
- **External-service paths** — GitHub, Sourcegraph, and MCP endpoints are never contacted. Env-gated fixtures (`claude_available → False`) keep assess deterministic and offline.
- **Real agent binaries** — `FakeAdapter` stands in for `claude`/`codex`/`copilot`. The adapter-protocol contract (`tests/test_adapter_contracts.py`) covers the real binaries separately.
- **EnterpriseBench TOML-shape tasks** — Registered in `fixtures.py` but not yet exercised by a dedicated capability test because the current validate/run surface assumes directory-shaped tasks. Tracked as follow-up when the writer / oracle-loader learns the TOML-only shape.

## Follow-up beads to open

None at the moment — no capability baseline is failing on clean `main`. If a
follow-up bead surfaces a broken capability after merge, file it with:

- Failing test name(s) from this matrix
- Capability + fixture tag from the assertion message
- Reproduction command: `pytest tests/capabilities/test_<cap>_capability.py -k <test_name> -v`

## Files added

```
tests/capabilities/
├── __init__.py
├── conftest.py
├── context.md                         # this file
├── fixtures.py
├── README.md
├── test_assess_capability.py
├── test_e2e_capability.py
├── test_mine_capability.py
├── test_run_capability.py
└── test_validate_capability.py
```

No changes to `src/` or `pyproject.toml` — the new directory slots cleanly
into the existing pytest test path.
