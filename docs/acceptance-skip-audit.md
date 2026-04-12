# Acceptance Criteria Skip Audit

Audit of the 16 perpetually-skipped criteria observed in acceptance loop runs.
Cross-referenced from `/tmp/codeprobe-loop-20260412-143716/iter-3/verdict.json`
against `acceptance/criteria.toml` and `acceptance/verify.py`.

## Summary

- **Total criteria:** 25
- **Passing:** 8 (6 structural, 2 behavioral)
- **Failing:** 1 (BUG-INIT-DEFAULT-006)
- **Skipped:** 16 (2 structural, 9 behavioral, 5 statistical)

## Categorization

| id                         | check_type          | tier        | skip reason                                               | category                          | action                                           |
| -------------------------- | ------------------- | ----------- | --------------------------------------------------------- | --------------------------------- | ------------------------------------------------ |
| BUG-MINE-RUN-001           | cli_exit_code       | behavioral  | workspace artifact missing (`.exit` file)                 | real-run-only                     | `eval_mode_required = "full"` added              |
| BUG-OUT-FLAG-002           | cli_help_contains   | behavioral  | workspace artifact missing (`.stdout` file)               | real-run-only                     | `eval_mode_required = "full"` added              |
| BUG-INTERPRET-STDOUT-003   | stream_separation   | behavioral  | unsupported check_type + needs workspace                  | compiler-will-fix + real-run-only | `eval_mode_required = "full"` added; handler TBD |
| BUG-INTERPRET-EXIT-004     | cli_exit_code       | behavioral  | workspace artifact missing (`.exit` file)                 | real-run-only                     | `eval_mode_required = "full"` added              |
| BUG-VALIDATE-DISCOVERY-005 | cli_stdout_contains | behavioral  | workspace artifact missing (`.stdout` file)               | real-run-only                     | `eval_mode_required = "full"` added              |
| SILENT-MINE-COUNT-001      | count_ge            | statistical | workspace source dir missing; depends on BUG-MINE-RUN-001 | real-run-only + dependent         | `eval_mode_required = "full"` added              |
| SILENT-RUN-RESULTS-002     | json_count_ge       | statistical | workspace JSON artifact missing                           | real-run-only                     | `eval_mode_required = "full"` added              |
| SILENT-CANARY-003          | canary_detect       | statistical | canary.txt not present in workspace                       | real-run-only + dependent         | `eval_mode_required = "full"` added              |
| TELEM-COST-SOURCE-001      | json_field_not_null | statistical | workspace JSON artifact missing                           | real-run-only + dependent         | `eval_mode_required = "full"` added              |
| TELEM-COST-USD-002         | json_field_type     | statistical | workspace JSON artifact missing                           | real-run-only + dependent         | `eval_mode_required = "full"` added              |
| LOG-VERBOSE-DEBUG-001      | log_level_matches   | behavioral  | unsupported check_type                                    | compiler-will-fix + real-run-only | `eval_mode_required = "full"` added; handler TBD |
| LOG-QUIET-WARNING-002      | log_level_matches   | behavioral  | unsupported check_type                                    | compiler-will-fix + real-run-only | `eval_mode_required = "full"` added; handler TBD |
| LOG-STDERR-003             | stream_separation   | behavioral  | unsupported check_type                                    | compiler-will-fix + real-run-only | `eval_mode_required = "full"` added; handler TBD |
| OUT-JSON-LINES-001         | json_lines_valid    | behavioral  | unsupported check_type                                    | compiler-will-fix + real-run-only | `eval_mode_required = "full"` added; handler TBD |
| OUT-ROUNDTRIP-002          | dataclass_roundtrip | structural  | unsupported check_type                                    | compiler-will-fix                 | handler TBD (structural, no workspace needed)    |
| CI-SAME-IMAGE-002          | yaml_field_equal    | structural  | unsupported check_type                                    | compiler-will-fix                 | handler TBD (structural, no workspace needed)    |

## Categories

- **real-run-only** (9 primary): Criteria whose check handler exists but requires workspace artifacts that only a full agent run produces (`.exit`, `.stdout`, `.stderr` files, JSON results, canary). Addressed by adding `eval_mode_required = "full"` so the verifier skips with a clear reason instead of an opaque "artifact missing" message.

- **compiler-will-fix** (7 total, 5 also real-run-only): Criteria whose `check_type` has no handler registered in `Verifier._handlers()`. Missing handlers: `stream_separation`, `log_level_matches`, `json_lines_valid`, `dataclass_roundtrip`, `yaml_field_equal`. These should be implemented in a follow-up bead.

- **dependent** (4): Criteria that also depend on other skipped criteria via `depends_on`. These would cascade-skip even if their own check could run, because their dependency was skipped first. All are also `real-run-only`.

- **retire**: None. All 25 criteria encode valid contracts from the PRDs.

## Changes Made

1. Added `eval_mode_required: str | None` field to `Criterion` dataclass in `acceptance/loader.py`
2. Updated `_parse_entry()` in loader to read `eval_mode_required` from TOML
3. Added `eval_mode: str | None` parameter to `Verifier.__init__()` in `acceptance/verify.py`
4. Added `_check_eval_mode()` method that skips with evidence `"eval_mode mismatch: requires 'full', current is 'none'"` when the mode does not match
5. Tagged 14 criteria with `eval_mode_required = "full"` in `acceptance/criteria.toml` (all behavioral/statistical that need workspace artifacts)
6. 2 structural criteria (`OUT-ROUNDTRIP-002`, `CI-SAME-IMAGE-002`) left without `eval_mode_required` since they do not need a workspace; they skip only because the handler is unimplemented

## Follow-up Work

- Implement missing check_type handlers: `stream_separation`, `log_level_matches`, `json_lines_valid`, `dataclass_roundtrip`, `yaml_field_equal`
- Wire `eval_mode` through the acceptance loop runner so full runs pass `eval_mode="full"`
