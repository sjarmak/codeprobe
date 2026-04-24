# Appendix: Error Migration Mapping

Tracks the per-file replacement of bare `click.UsageError` / `click.ClickException` / `raise SystemExit(<int>)` / `sys.exit(<int>)` call sites with typed `PrescriptiveError` / `DiagnosticError` from `codeprobe.cli.errors`.

The `old_lineno` column refers to the pre-migration line numbers recorded in `tests/cli/test_no_bare_usage_errors.py::INITIAL_WHITELIST`.

Every `new_code` listed here is present in `src/codeprobe/cli/error_codes.json` and is enforced by `tests/cli/test_error_codes_drift.py`.

## calibrate_cmd.py

| file | old_lineno | old_expression | new_code | new_kind | new_terminal | notes |
|------|-----------:|---------------|----------|----------|:---:|------|
| `src/codeprobe/cli/calibrate_cmd.py` | 115 | `raise SystemExit(1) from exc` | `CALIBRATION_REJECTED` | diagnostic | true | Emits via DiagnosticError; pretty "calibration_rejected: …" banner moves to handler. |

## doctor_cmd.py

| file | old_lineno | old_expression | new_code | new_kind | new_terminal | notes |
|------|-----------:|---------------|----------|----------|:---:|------|
| `src/codeprobe/cli/doctor_cmd.py` | 128 | `raise SystemExit(1)` (pretty path) | `DOCTOR_CHECKS_FAILED` | diagnostic | true | Check rows still echo; checks list propagates via `detail._envelope_data`. |
| `src/codeprobe/cli/doctor_cmd.py` | 143 | `raise SystemExit(exit_code)` (envelope path) | `DOCTOR_CHECKS_FAILED` | diagnostic | true | Single envelope now emitted by the top-level handler (no double-emit). Exit code moves from 1→2 per catalog. |

## check_infra.py

| file | old_lineno | old_expression | new_code | new_kind | new_terminal | notes |
|------|-----------:|---------------|----------|----------|:---:|------|
| `src/codeprobe/cli/check_infra.py` | 57 | `raise click.ClickException(f"metadata.json not found …")` | `METADATA_MISSING` | diagnostic | true | Diagnose: `codeprobe check-infra drift <dir> --json`. |
| `src/codeprobe/cli/check_infra.py` | 61 | `raise click.ClickException("metadata.json … is not valid JSON")` | `METADATA_INVALID` | diagnostic | true | JSON decode errors at trust boundary. |
| `src/codeprobe/cli/check_infra.py` | 76 | `raise click.ClickException("… must be a JSON array")` | `METADATA_INVALID` | diagnostic | true | Schema violation. |
| `src/codeprobe/cli/check_infra.py` | 81 | `raise click.ClickException("entries must be strings")` | `METADATA_INVALID` | diagnostic | true | Schema violation. |
| `src/codeprobe/cli/check_infra.py` | 134 | `raise SystemExit(1)` (envelope path) | `CAPABILITY_DRIFT` | diagnostic | true | `detail._envelope_data` carries drift diff. |
| `src/codeprobe/cli/check_infra.py` | 156 | `raise click.ClickException(message)` (pretty path) | `CAPABILITY_DRIFT` | diagnostic | true | Same semantics as 134; pretty branch. |
| `src/codeprobe/cli/check_infra.py` | 330 | `raise click.ClickException("Unknown backend(s) …")` | `UNKNOWN_BACKEND` | prescriptive | false | `--backend <first-known>`. |
| `src/codeprobe/cli/check_infra.py` | 337 | `raise click.ClickException("No LLM backends configured …")` | `NO_BACKENDS_CONFIGURED` | diagnostic | true | Diagnose: `codeprobe doctor --json`. |
| `src/codeprobe/cli/check_infra.py` | 382 | `raise click.ClickException("Offline pre-flight failed …")` | `OFFLINE_PREFLIGHT_FAILED` | diagnostic | true | Also raised by `codeprobe run --offline`. |

## snapshot_cmd.py

| file | old_lineno | old_expression | new_code | new_kind | new_terminal | notes |
|------|-----------:|---------------|----------|----------|:---:|------|
| `src/codeprobe/cli/snapshot_cmd.py` | 77 | `raise click.UsageError("unknown --scanner …")` | `UNKNOWN_BACKEND` | prescriptive | false | `--scanner pattern`. |
| `src/codeprobe/cli/snapshot_cmd.py` | 178 | `sys.exit(2)` (redact without ack) | `SOURCE_EXPORT_REQUIRES_ACK` | prescriptive | false | `--allow-source-in-export`. |
| `src/codeprobe/cli/snapshot_cmd.py` | 194 | `sys.exit(3)` (canary proof passed=false) | `CANARY_PROOF_FAILED` | diagnostic | true | Diagnose: `codeprobe doctor`. |
| `src/codeprobe/cli/snapshot_cmd.py` | 204 | `sys.exit(4)` (non-TTY, no canary proof) | `CANARY_PROOF_REQUIRED` | prescriptive | false | `--canary-proof <path>`. |
| `src/codeprobe/cli/snapshot_cmd.py` | 212 | `sys.exit(5)` (interactive canary mismatch) | `CANARY_MISMATCH` | diagnostic | true | Diagnose: `codeprobe doctor`. |
| `src/codeprobe/cli/snapshot_cmd.py` | 218 | `sys.exit(6)` (CanaryGate failure) | `CANARY_GATE_FAILED` | diagnostic | true | Diagnose: `codeprobe doctor --json`. |
| `src/codeprobe/cli/snapshot_cmd.py` | 238 | `sys.exit(7)` (create_snapshot exception) | `SNAPSHOT_CREATE_FAILED` | diagnostic | true | Covers PermissionError / CanaryFailed / ScannerUnavailable / FileNotFoundError / SymlinkEscapeError. |
| `src/codeprobe/cli/snapshot_cmd.py` | 289 | `sys.exit(1)` (verify_snapshot_extended !ok) | `SNAPSHOT_VERIFY_FAILED` | diagnostic | true | Diagnose: `codeprobe snapshot verify --verbose`. |
| `src/codeprobe/cli/snapshot_cmd.py` | 361 | `raise click.UsageError("unknown --format …")` | `UNKNOWN_BACKEND` | prescriptive | false | Defensive — click.Choice usually guards this. `--format <first-known>`. |
| `src/codeprobe/cli/snapshot_cmd.py` | 364 | `sys.exit(8)` (export FileNotFoundError) | `METADATA_MISSING` | diagnostic | true | Missing artefact in snapshot dir. |

## run_cmd.py

| file | old_lineno | old_expression | new_code | new_kind | new_terminal | notes |
|------|-----------:|---------------|----------|----------|:---:|------|
| `src/codeprobe/cli/run_cmd.py` | 271 | `raise SystemExit(1)` (show_prompt: no exp) | `NO_EXPERIMENT` | diagnostic | true | next_steps: Initialize. |
| `src/codeprobe/cli/run_cmd.py` | 296 | `raise SystemExit(1)` (show_prompt: no tasks) | `NO_TASKS` | diagnostic | true | next_steps: mine + run. |
| `src/codeprobe/cli/run_cmd.py` | 423 | `raise SystemExit(1)` (multiple experiments) | `AMBIGUOUS_EXPERIMENT` | prescriptive | false | `--config <first-candidate>`. |
| `src/codeprobe/cli/run_cmd.py` | 430 | `raise SystemExit(1)` (no experiment) | `NO_EXPERIMENT` | diagnostic | true | next_steps: Initialize. |
| `src/codeprobe/cli/run_cmd.py` | 436 | `raise SystemExit(1)` (unknown agent) | `UNKNOWN_BACKEND` | prescriptive | false | `--agent claude`. |
| `src/codeprobe/cli/run_cmd.py` | 466 | `raise SystemExit(1)` (no tasks) | `NO_TASKS` | diagnostic | true | next_steps: mine + run. |
| `src/codeprobe/cli/run_cmd.py` | 480 | `raise SystemExit(1)` (suite matched 0) | `NO_SUITE_MATCH` | diagnostic | true | Diagnose: `codeprobe run --dry-run <path>`. |
| `src/codeprobe/cli/run_cmd.py` | 565 | `raise SystemExit("Error: invalid permission_mode …")` | `INVALID_PERMISSION_MODE` | prescriptive | false | `--permission-mode default`. |
| `src/codeprobe/cli/run_cmd.py` | 719 | `raise SystemExit(130)` (KeyboardInterrupt) | `INTERRUPTED` | diagnostic | true | Exit code preserved at 130. Diagnose: `codeprobe run … --resume`. |
| `src/codeprobe/cli/run_cmd.py` | 778 | `raise SystemExit(3)` (TraceBudgetExceeded) | `TRACE_BUDGET_EXCEEDED` | prescriptive | false | `--trace-overflow truncate` (current surface). Exit code moves 3→2 per catalog. |

## mine_cmd.py

| file | old_lineno | old_expression | new_code | new_kind | new_terminal | notes |
|------|-----------:|---------------|----------|----------|:---:|------|
| `src/codeprobe/cli/mine_cmd.py` | 73 | `raise click.UsageError("scheme … not accepted")` | `INVALID_GIT_URL` | prescriptive | false | `--paths-or-https-url`. |
| `src/codeprobe/cli/mine_cmd.py` | 82 | `raise click.UsageError("missing host")` | `INVALID_GIT_URL` | prescriptive | false | Same. |
| `src/codeprobe/cli/mine_cmd.py` | 86 | `raise click.UsageError("missing repository path")` | `INVALID_GIT_URL` | prescriptive | false | Same. |
| `src/codeprobe/cli/mine_cmd.py` | 103 | `raise click.UsageError("private/link-local address")` | `INVALID_GIT_URL` | prescriptive | false | SSRF guard. |
| `src/codeprobe/cli/mine_cmd.py` | 135 | `raise click.UsageError("Could not clone …")` | `CLONE_FAILED` | diagnostic | true | Diagnose: `gh repo view <url>`. |
| `src/codeprobe/cli/mine_cmd.py` | 143 | `raise click.UsageError("Clone … timed out")` | `CLONE_FAILED` | diagnostic | true | Same. |
| `src/codeprobe/cli/mine_cmd.py` | 634 | `raise click.UsageError("Path is not a directory")` | `INVALID_GIT_URL` | prescriptive | false | Repo-path validation. |
| `src/codeprobe/cli/mine_cmd.py` | 639 | `raise click.UsageError("Not a git repository")` | `INVALID_GIT_URL` | prescriptive | false | Repo-path validation. |
| `src/codeprobe/cli/mine_cmd.py` | 676 | `raise click.UsageError("not a valid git URL")` | `INVALID_GIT_URL` | prescriptive | false | Safety net after shape check. |
| `src/codeprobe/cli/mine_cmd.py` | 681 | `raise click.UsageError("Path does not exist")` | `INVALID_GIT_URL` | prescriptive | false | Includes `Did you mean` hint. |
| `src/codeprobe/cli/mine_cmd.py` | 1082 | `raise click.UsageError("Profile … not found")` | `UNKNOWN_BACKEND` | prescriptive | false | `--profile <first-available>`. |
| `src/codeprobe/cli/mine_cmd.py` | 1170 | `raise click.UsageError("Unknown preset …")` | `UNKNOWN_BACKEND` | prescriptive | false | `--preset <first-known>`. |
| `src/codeprobe/cli/mine_cmd.py` | 1183 | `raise click.UsageError("Cannot use both --preset and --goal …")` | `MUTEX_FLAGS` | prescriptive | false | `--preset` (drop deprecated). |
| `src/codeprobe/cli/mine_cmd.py` | 1197 | `raise click.UsageError("Unknown goal …")` | `UNKNOWN_BACKEND` | prescriptive | false | `--goal <first-known>`. |
| `src/codeprobe/cli/mine_cmd.py` | 1328 | `raise click.UsageError("--cross-repo path does not exist")` | `INVALID_GIT_URL` | prescriptive | false | Includes `Did you mean`. |
| `src/codeprobe/cli/mine_cmd.py` | 1562 | `raise click.UsageError("--narrative-source parsed empty")` | `NARRATIVE_SOURCE_UNDETECTABLE` | prescriptive | false | `--narrative-source commits`. |
| `src/codeprobe/cli/mine_cmd.py` | 1569 | `raise click.UsageError(str(exc))` (bad adapter name) | `NARRATIVE_SOURCE_UNDETECTABLE` | prescriptive | false | Same. |
| `src/codeprobe/cli/mine_cmd.py` | 1587 | `raise click.UsageError("No merged PR narratives …")` | `NARRATIVE_SOURCE_UNDETECTABLE` | prescriptive | false | INV1 explicit-selection trigger. |
| `src/codeprobe/cli/mine_cmd.py` | 2028 | `raise click.UsageError("--cross-repo with --org-scale")` | `MUTEX_FLAGS` | prescriptive | false | `--org-scale`. |
| `src/codeprobe/cli/mine_cmd.py` | 2090 | `raise click.UsageError("--backends agent with --no-llm")` | `MUTEX_FLAGS` | prescriptive | false | `--no-llm`. |
| `src/codeprobe/cli/mine_cmd.py` | 2151 | `raise click.UsageError("--repos path does not exist")` | `INVALID_GIT_URL` | prescriptive | false | Includes `Did you mean`. |
| `src/codeprobe/cli/mine_cmd.py` | 2238 | `sys.exit(130)` (KeyboardInterrupt) | `INTERRUPTED` | diagnostic | true | Exit code 130. Diagnose: `codeprobe mine … --resume`. |
| `src/codeprobe/cli/mine_cmd.py` | 2730 | `raise click.UsageError("--refresh target is not a directory")` | `INVALID_GIT_URL` | prescriptive | false | Refresh mode path check. |
| `src/codeprobe/cli/mine_cmd.py` | 2739 | `raise click.UsageError(str(exc))` (metadata.json missing) | `METADATA_MISSING` | diagnostic | true | Diagnose: `codeprobe check-infra drift <dir> --json`. |
| `src/codeprobe/cli/mine_cmd.py` | 2744 | `raise click.UsageError(str(exc))` (structural signature missing) | `METADATA_MISSING` | diagnostic | true | Same. |
| `src/codeprobe/cli/mine_cmd.py` | 2750 | `raise click.UsageError("Could not resolve HEAD …")` | `INVALID_GIT_URL` | prescriptive | false | Refresh mode HEAD resolution. |
| `src/codeprobe/cli/mine_cmd.py` | 2800 | `sys.exit(2)` (StructuralMismatchError) | `METADATA_INVALID` | diagnostic | true | Refresh integrity violation. |

## Handler installation

`src/codeprobe/cli/_error_handler.py` defines `CodeprobeGroup`, a `click.Group` subclass whose `.invoke()` override catches `CodeprobeError` and calls `render_codeprobe_error(ctx, exc)` before `ctx.exit(exc.exit_code)`.

Applied to three groups:

- The root `@click.group(cls=CodeprobeGroup)` in `src/codeprobe/cli/__init__.py`.
- The `@click.group(cls=CodeprobeGroup)` for `snapshot` in `src/codeprobe/cli/snapshot_cmd.py`.
- The `@click.group(name="check-infra", cls=CodeprobeGroup)` in `src/codeprobe/cli/check_infra.py`.

The subgroup overrides matter for tests that invoke the subgroup directly through `CliRunner().invoke(snapshot, …)` / `…invoke(check_infra, …)` — without them the typed error escapes as a raw traceback.

### Output mode cross-talk

`src/codeprobe/cli/_output_helpers.py::resolve_mode()` stashes the resolved `OutputMode` on the root context's `obj` dict under `codeprobe_output_mode`. The error handler reads this first so a subcommand's `--json` / `--no-json` / `--json-lines` choice is honoured even after the subcommand's own context has been torn down by click's nested invocation stack.

### Envelope data for error envelopes

`CodeprobeError.detail` may include a reserved key `_envelope_data`. The handler strips it from the serialised `error.detail` and promotes the value into the envelope's top-level `data` block. Doctor and `check-infra drift` use this to surface structured check / drift data inside the error envelope without needing a second envelope emission. Callers that don't set `_envelope_data` get `data={}` as before.
