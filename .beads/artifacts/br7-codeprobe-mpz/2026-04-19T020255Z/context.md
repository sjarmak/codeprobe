# Worker context for codeprobe-mpz (br7.8 — Task-type selection CLI)

## What the bead asked for

Expose the already-landed task-type taxonomy as a first-class CLI surface
on `codeprobe mine`: `--list-task-types` should show the supported set
with descriptions and CodeScaleBench suite mappings; `--task-type <TYPE>`
should validate, route to the correct mining pipeline, and warn when the
selected type is a poor fit for the target repo. Registration must be
data-driven (dataclass/dict), not hardcoded `if/else`. No new task types
were invented beyond what the cherry-picks landed.

## What I did

- `src/codeprobe/mining/task_types.py` (new, ~175 lines): single
  source of truth for task-type metadata. `TaskTypeInfo` dataclass
  holds name, description (>=40 chars enforced in `__post_init__`),
  primary CSB suite, all mapped CSB suites, and a `dispatch_key`
  routing token. `TASK_TYPE_REGISTRY` enumerates the 6 types (5
  persisted `TASK_TYPES` from `models/task.py` + `mixed` meta-type).
- `src/codeprobe/cli/__init__.py`: added `--list-task-types` (eager
  flag, short-circuits before any I/O) and `--task-type` (Click Choice
  against `task_type_names()`). Both pass through to `run_mine`.
- `src/codeprobe/cli/mine_cmd.py`:
  - `run_mine` accepts `task_type_override`; when set, it wins over
    the `--goal`-derived task type and auto-enables `--org-scale` for
    the `org_scale_cross_repo` type so `--task-type` alone is
    sufficient.
  - `_dispatch_by_task_type` refactored: instead of a 4-branch
    `if/elif` chain, it looks up `info.dispatch_key` in the registry
    and dispatches through a small `_DISPATCH_HANDLERS` dict. Adding a
    new task type that reuses an existing pipeline is now purely a
    registry data change.
  - `_suitability_warnings` + `_run_suitability_check` added. Cheap
    `Path.rglob` sampling (capped at 2000 entries) detects tiny-repo
    edge cases for `org_scale_cross_repo`, `mcp_tool_usage`,
    `architecture_comprehension`, and missing `tests/` for
    `sdlc_code_change`. Non-blocking by default; prompts for
    continuation only when interactive.
- `tests/test_task_types.py` (new, 17 tests): registry shape,
  description length, primary suite ∈ suites tuple, model
  TASK_TYPES alignment, CSB suite grounding against
  `csb-v2-dual264.json`, CLI end-to-end (list, invalid, valid →
  `run_mine.task_type_override`), suitability warnings for all four
  warning classes, and interactive decline behavior.

## Acceptance criteria (self-check)

- [x] `--list-task-types` works — prints all 6 types with `>= 40`-char
  descriptions and a concrete CSB suite; see
  `transcript_list_task_types.txt`. Reviewer can cross-check the 20
  suite mappings against `csb-v2-dual264.json`: `suites` key.
- [x] `--task-type` accepts valid, rejects invalid non-zero —
  `transcript_invalid_task_type.txt` shows Click echoing the full
  valid set on rejection.
- [x] Type-specific output differs meaningfully —
  `transcript_orgscale_on_codeprobe.txt` (5 file-list oracle tasks
  from 5 pattern families) vs. `transcript_probe_on_codeprobe.txt`
  (3 integer/symbol probes). Compare
  `sample_orgscale_instruction.md` (multi-section task contract,
  file-path answer format) vs. `sample_probe_instruction.md`
  (one-line count question). Different pipelines, different oracle
  types (`file_list` vs. `count`), different instruction shapes.
- [x] Suite mapping is real — automated in
  `test_task_types.py::test_all_registered_suites_exist_in_csb_dual264`
  (all 20 mapped suite IDs present). Verified manually:
  `python3 -c "import json; print('csb_sdlc_fix' in json.load(open('/home/ds/projects/CodeScaleBench/benchmarks/suites/csb-v2-dual264.json'))['suites'])"` → `True`.
- [x] Suitability pre-check exists and works — see
  `transcript_orgscale_on_tiny.txt` (tiny repo → warning) vs.
  `transcript_orgscale_on_codeprobe.txt` (215 .py files → no
  warning). Tests cover both paths.
- [x] Data-driven registration — `TASK_TYPE_REGISTRY` dict keyed by
  name; `_DISPATCH_HANDLERS` dict keyed by `dispatch_key`. The only
  `if/elif` remaining is the org-scale short-circuit in `run_mine`,
  which is architectural (different pipeline with multi-repo
  scanning), not per-task-type.
- [x] Dogfooding evidence — 5 transcripts in this directory cover
  `--list-task-types`, invalid-type rejection, suitability warning
  firing on a tiny repo, and two meaningfully different mining runs
  on the same codebase.
- [x] No regressions — `pytest tests/ -q` → 2273 passed, 1 skipped,
  1 warning (pre-existing `TestAction` collection warning unrelated
  to this change).

## Oracle references I consulted

- `/home/ds/projects/CodeScaleBench/benchmarks/suites/csb-v2-dual264.json`
  for the authoritative CSB suite IDs used in `TaskTypeInfo.csb_suite`
  / `csb_suites`. The test
  `test_all_registered_suites_exist_in_csb_dual264` keeps the mapping
  honest.
- `src/codeprobe/models/task.py::TASK_TYPES` as the authoritative
  frozenset of persisted types; the test
  `test_all_registered_types_except_mixed_are_in_model_task_types`
  enforces alignment. `mixed` is intentionally a CLI-layer meta-type
  — it never lands on a persisted `Task.metadata.task_type`; the
  `_dispatch_mixed` path splits into `sdlc` + `micro_probe` tasks.
- `mining/org_scale_families.py` (FAMILIES tuple) for the 6 Phase-1/2
  org-scale family patterns that `org_scale_cross_repo` dispatches to
  — confirmed via `transcript_orgscale_on_codeprobe.txt`.

## Known limitations

- Suitability thresholds (50 files for org-scale, 100 for MCP, 20 for
  comprehension) are heuristic defaults. Per ZFC, the file-count
  signal is structural, but the thresholds themselves are arbitrary
  — tunable, not load-bearing. The warnings are advisory, not gating.
- The refactor keeps the `--org-scale` flag as a separate code path
  because it has a materially different signature (multi-repo
  scanning, curation backends). `--task-type org_scale_cross_repo`
  auto-enables `org_scale=True` so the user-facing surface stays
  clean.
- `--task-type` is only checked against the registry at the CLI
  layer (Click Choice). Programmatic callers of `run_mine` passing
  `task_type_override=` get no runtime validation; they'd dispatch
  through the "unknown → SDLC fallback" branch and a warning log.
  Acceptable: this is internal API, and the tests cover the CLI path.
- Per-type scoring rubrics are explicitly out of scope (bead's "Not
  in scope"). The registry just maps to suites; scoring stays in the
  existing verifier modules.

## Files the reviewer should read

In order:

1. `src/codeprobe/mining/task_types.py` — registry, the single
   extension point for new types.
2. `src/codeprobe/cli/__init__.py` — search `--list-task-types` /
   `--task-type` to see the two new options and their short-circuit
   handler.
3. `src/codeprobe/cli/mine_cmd.py` — `_suitability_warnings`,
   `_run_suitability_check`, and the refactored
   `_dispatch_by_task_type`. Also the `task_type_override` wiring in
   `run_mine`.
4. `tests/test_task_types.py` — 17 tests, organized into
   registry-shape / CSB-grounding / CLI / suitability sections.
5. This directory's transcript files, as listed in the acceptance
   criteria check above.
