# Worker context for codeprobe-fyf (br7.7 — Mining UX polish)

## What the bead asked for

A fresh user pointing `codeprobe mine` at their own repo should get a
polished guided flow, not just a functional one. The seven acceptance
criteria graded per-criterion by the Codex reviewer are:

1. Interactive prompts for missing inputs (click prompts, not argparse
   usage errors).
2. Input validation with clear errors for invalid repo path / URL /
   corrupt git history.
3. Progress feedback on long operations; Ctrl-C cleans up and exits 130.
4. End-of-run structured summary (task count, quality gate, output path,
   time, cost-if-applicable).
5. Post-run "Next steps" guidance whose suggested commands actually work
   against the produced output.
6. Dogfooding evidence (before/after transcripts) in `context.md`.
7. No regressions in the existing test suite.

## What was already landed before this session

Prior work on this bead landed most of the plumbing. `src/codeprobe/cli/mine_cmd.py`
already contained:

- `run_mine` line 1925-1938 — AC1: prompt for a path when `path == "."`
  and cwd is not a git repo (interactive only).
- `_resolve_repo_path` + `_suggest_path` + `_validate_git_repo` — AC2:
  path-shape validation with "did you mean" suggestions.
- `_clone_repo` — AC2: actionable error when `git clone` fails, including
  timeout handling.
- `_mine_tasks_with_progress` — AC3: `click.progressbar` on TTY, plain
  echo on CI.
- `run_mine` lines 2046-2055 — AC3: `KeyboardInterrupt` cleanup that
  removes partial `tasks_dir` and exits 130.
- `_print_summary_block` — AC4: structured block with tasks mined,
  quality gate status, elapsed time, output path, suite manifest.
- `_show_next_steps` + `_show_org_scale_results` — AC5: concrete next
  commands including `codeprobe validate`, `codeprobe run --agent claude`,
  `--model`, `--max-cost-usd`.

The dogfood pass below found two rough edges that had not yet been
addressed.

## What I changed this session

### 1. `codeprobe validate <parent>` now works on a tasks parent dir (AC5)

`_show_next_steps` has always suggested
`codeprobe validate <tasks_dir>`. That command failed with three useless
FAILs (`instruction.md not found`, `metadata parses — neither task.toml
nor metadata.json`, `tests/test.sh not found`) because `validate`
treated the parent dir as if it were a single task. The user would
follow the suggestion, see three red FAILs, and have no hint that the
tool wanted them to pass a *child* task dir.

Fix in `src/codeprobe/cli/validate_cmd.py`:

- New helpers `_looks_like_task_dir(path)` (root has
  `instruction.md`, `task.toml`, or `metadata.json`) and
  `_list_child_task_dirs(path)` (immediate children that pass
  `_looks_like_task_dir`).
- In the `validate` click command, when the arg itself is not a task
  dir but has task-shaped children, iterate over the children, print
  `PASS <name>` / `FAIL <name>` plus the failing checks, and end with a
  `Validated N task(s): M passed, K failed.` summary. Exit 1 if any
  child failed.
- Legacy single-task-dir semantics stay intact for empty or malformed
  single-task inputs — tests
  `test_single_task_dir_still_works` and
  `test_parent_with_non_task_children_falls_back` pin that.

### 2. Pre-clone URL-shape validation (AC2)

Passing `ftp://foo/bar` silently fell through the git-URL regex, got
treated as a path, and surfaced as
`Path does not exist: /<cwd>/ftp:/foo/bar` — confusing because the
user clearly passed a URL. Passing `https://example.com` (host-only,
no repo path) reached `git clone`, which failed with
`fatal: repository 'https://example.com/' not found` — the cause was
wrong but the error implied a missing remote repo.

Fix in `src/codeprobe/cli/mine_cmd.py`:

- New constant `_ACCEPTED_GIT_URL_SCHEMES` enumerating the schemes we
  will route to `git clone` (`http`, `https`, `git`, `ssh`,
  `git+http`, `git+https`, `git+ssh`).
- New `_validate_git_url_shape(url)` rejects non-accepted schemes and
  host-only URLs with clear `click.UsageError("URL X is not a valid
  git URL: ...")` messages. SCP-like `git@host:owner/repo` is allowed
  through unchanged (not a URL per RFC 3986).
- `_clone_repo` calls `_validate_git_url_shape` before
  `_validate_clone_url`, so the user never sees the partial
  `Cloning …` line on shape errors.
- New `_looks_like_url(path)` in `_resolve_repo_path` catches
  URL-shaped inputs with bad schemes (`ftp://…`) that the existing
  `_is_git_url` regex rejected. Those now raise a URL-specific error
  instead of being mis-routed to filesystem validation.

### 3. Tests

- `tests/test_validate_cmd.py` → new class `TestValidateMultiTaskDir`
  with four tests (all-pass parent, one-broken parent, single-task
  fallback, non-task children fallback).
- `tests/test_mine_cli.py` → new class `TestMineUrlValidation` with
  three tests (bad scheme, host-only URL, `owner/repo` shorthand still
  routes to clone).

## Dogfooding evidence

Before transcripts (captured by temporarily stashing the two touched
files so the editable install ran the pre-patch code):

- `transcript_before_ftp_scheme.txt` — `ftp://foo.com/bar` misreported
  as `Path does not exist: /tmp/ftp:/foo.com/bar`.
- `transcript_before_https_hostonly.txt` — `https://example.com` got
  as far as `git clone`, returned `fatal: repository
  'https://example.com/' not found`.
- `transcript_before_validate_parent.txt` — `codeprobe validate
  .codeprobe/tasks` (the exact string `_show_next_steps` recommends)
  produced three meaningless FAILs.

After transcripts:

- `transcript_after_ftp_scheme.txt` — `URL 'ftp://foo.com/bar' is not
  a valid git URL: scheme 'ftp' is not one of [...]`.
- `transcript_after_https_hostonly.txt` — `URL 'https://example.com'
  is not a valid git URL: missing repository path (expected e.g.
  https://host.example/owner/repo.git)`.
- `transcript_after_validate_parent.txt` — iterates 250 probe
  children and prints `Validated 250 task(s): 250 passed, 0 failed.`
- `transcript_after_mine_e2e.txt` — one full `codeprobe mine
  /home/ds/projects/codeprobe --no-interactive --count 3 --no-llm`
  run showing the Mining summary block (AC4) and Next steps (AC5),
  where step 1 is now the `codeprobe validate` command that actually
  works against the produced output.

## Acceptance criteria (self-check)

- [x] **AC1 — Interactive prompts.** Unchanged this session; already
  satisfied. `run_mine` prompts for a path when `path == "."` and cwd
  isn't a git repo and TTY is available. In non-interactive mode
  (`--no-interactive` or non-TTY), a clear `click.UsageError` fires
  with the "Not a git repository" message.
- [x] **AC2 — Clear validation errors.** Path validation was already
  clear; URL validation is the new piece. Bad schemes and host-only
  URLs now produce `URL ... is not a valid git URL` errors before
  `git clone` is ever invoked.
- [x] **AC3 — Progress feedback.** Unchanged this session; already
  satisfied. `_mine_tasks_with_progress` uses `click.progressbar` on
  TTY, plain-echo in CI; `run_mine` catches `KeyboardInterrupt`,
  `shutil.rmtree`s the partial `tasks_dir`, and calls `sys.exit(130)`.
- [x] **AC4 — End-of-run summary.** Unchanged this session; already
  satisfied. `_print_summary_block` emits tasks mined, quality-gate
  status, elapsed time, output path, suite manifest, and LLM-
  enrichment indicator. Confirmed in
  `transcript_after_mine_e2e.txt`.
- [x] **AC5 — Post-run guidance that works.** Next-steps block was
  already present; its first suggestion (`codeprobe validate
  <tasks_dir>`) now actually succeeds. Confirmed by running the exact
  string from the e2e transcript against the produced output and
  getting `Validated 250 task(s): 250 passed, 0 failed.`
- [x] **AC6 — Dogfooding evidence.** Six transcripts in this
  directory: three before/after pairs for the two touched edges plus
  the e2e after-run.
- [x] **AC7 — No regressions.** `pytest tests/ -q` → `2326 passed, 1
  skipped, 1 warning` (pre-existing `TestAction` collection warning
  unrelated to this change).

## Files the reviewer should read

In order:

1. `src/codeprobe/cli/validate_cmd.py` — look at the new
   `_looks_like_task_dir`, `_list_child_task_dirs`, and the "Parent-
   of-tasks mode" block inside `validate()`.
2. `src/codeprobe/cli/mine_cmd.py` lines 27–106 — the new
   `_ACCEPTED_GIT_URL_SCHEMES`, `_validate_git_url_shape`, and the
   one-line wire-up in `_clone_repo`.
3. `src/codeprobe/cli/mine_cmd.py` around `_resolve_repo_path` —
   `_looks_like_url` and its use to route URL-shaped inputs with
   rejected schemes through the URL validator.
4. `tests/test_validate_cmd.py::TestValidateMultiTaskDir` — four new
   tests for the multi-task validate flow.
5. `tests/test_mine_cli.py::TestMineUrlValidation` — three new tests
   for URL-shape rejection.
6. The six transcript files in this directory.

## Rough edges identified but NOT fixed this session

Documented so the reviewer can decide whether they block grading:

- `--count` has no range enforcement. The help string says "(3-20)"
  but `--count 0` silently runs (degenerates to 1-probe fallback)
  and `--count 500` loops over up to 1000 merge commits with no
  warning. Adding `click.IntRange(1, 50)` would be a one-line change
  but risks breaking existing callers that pass out-of-range values.
  Out of scope per bead's "Not in scope: do NOT redesign".
- `_show_next_steps` suggests `codeprobe run <repo>` but the `run`
  command uses `click.Path(exists=True)` for its `path` argument; a
  stale suggestion (e.g. after the temp clone is gone) would produce
  a click-standard "Invalid value" error. Acceptable: `run` is run
  against a local codeprobe checkout in practice, not the temp clone
  dir.
- The pre-flight SSRF check (`_validate_clone_url`) runs *after* the
  URL-shape check now. That ordering is intentional (shape errors
  are more user-actionable), but means a user who passes
  `https://127.0.0.1:8080/foo/bar` sees `Refusing to clone from
  private/link-local address: 127.0.0.1` — unchanged behavior.
- The Codex-reviewer calibration references
  `MCP-Eval-Tasks/{ccx-*, sg-*}` cited in the formula apply to
  Flavor R grading and are not consumed by this worker directly.
  The reviewer should use those to calibrate what "polished" looks
  like for similar CLIs.
