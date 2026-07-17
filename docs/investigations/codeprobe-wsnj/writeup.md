# codeprobe-wsnj — Delete stale pre-f22900f comprehension task dirs

Blast-radius follow-up from `codeprobe-lqct`. No source change in this repo;
the affected artifacts live outside the checkout under `~/projects/r0-repos/`
(gitignored, untracked). Filesystem-only cleanup, verified by direct
inspection rather than a test suite.

## Background

Commit `f22900f` fixed `_dispatch_comprehension` to write tasks via
`write_comprehension_tasks` (which emits `tests/ground_truth.json`); before
that fix, the generic `write_task_dir` emitted none, so every comprehension
task mined pre-fix was unscoreable (`ArtifactScorer` → `verifier_error`).
17 repos under `~/projects/r0-repos/` still held pre-fix `.codeprobe/tasks/`
dirs with 0 `ground_truth.json` files.

## What was verified before acting

```
cd ~/projects/r0-repos
for d in */.codeprobe/tasks; do
  echo "$(dirname $(dirname $d)) tasks=$(ls $d|wc -l) gt=$(find $d -name ground_truth.json|wc -l)"
done
```

Confirmed the exact split the bead described: `arrow attrs click flask httpx
jinja paramiko pexpect prompt_toolkit pygments schedule starlette tornado
tqdm typer uvicorn werkzeug` (17 repos) at `gt=0`; `gunicorn httpie isort
marshmallow requests rich sqlparse websockets` (8 repos, post-fix) and
`hyperfine` (unrelated) at `gt==tasks` (100% coverage).

## Action taken

Deleted `<repo>/.codeprobe/tasks/` for exactly the 17 named pre-fix repos.
Chose deletion over re-mining: `codeprobe mine --dual-verify` calls an LLM
per task plus dual-verify agent runs for ~172 tasks total, costly/long-running
for a P3 hygiene chore the bead itself frames as "a trap, not a crisis" with
no live consumer. The bead's own acceptance criteria accepts either outcome
("re-mined or deleted"). The 8 post-fix repos and `hyperfine` were left
untouched.

## Verification after acting

Re-ran the same split-check. Output: only the 9 already-clean repos
(`gunicorn httpie hyperfine isort marshmallow requests rich sqlparse
websockets`) remain under `.codeprobe/tasks/`, each still at `gt==tasks`.
Zero repos with `tasks>0 and gt==0` remain — acceptance criterion met.

## Follow-up filed

The bead's own notes flagged a structural gap this cleanup does not close:
`codeprobe mine`/`run` does not warn when a pre-existing `.codeprobe/tasks/`
dir is stale, so a future arm pointed at a repo with old pre-fix artifacts
would silently score every task `verifier_error` and read as agent
incompetence rather than a stale-artifact bug. Filed as `codeprobe-yxex`
(preflight guard: reject artifact_eval/dual tasks whose
`tests/ground_truth.json` is missing, with a prescriptive re-mine error)
rather than building it in scope here.
