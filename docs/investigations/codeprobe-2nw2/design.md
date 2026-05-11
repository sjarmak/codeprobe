# codeprobe-2nw2 — sg-only scaffold mode design

Phase 1 of the epic. This document is the contract that beads
`codeprobe-yw6u` (.2), `codeprobe-sm9f` (.3), and `codeprobe-hcnv`
(.4) implement against. Approved revisions (architect adversarial
review, 2026-05-11) fold in three CRITICAL and four HIGH fixes over
the original bead description; the divergences are called out
inline.

## Why

The codeprobe-jf28 SDLC v2 rerun showed the v2 sourcegraph preamble
underperforms baseline on SDLC by −0.087 mean reward when local
source is readable (with-sg-fixed). The agent over-explores via
Sourcegraph and still partial-fails edits the baseline reliably
gets. CodeScaleBench solves the same problem with a "truncate at
build time, restore at verify time" pattern: the workspace looks
like a tree of 0-byte placeholder files; the agent must read via
Sourcegraph MCP but can still write edits to the empty files.

Codeprobe doesn't use Docker per-task, so the equivalent has to
live in the Python isolation layer. The natural extension is to
give `quarantine_local_source` (added in codeprobe-jf28) a second
mode: instead of just hiding source, leave empty placeholders at
the original paths and overlay agent edits back at exit.

## Mode names and semantics

`quarantine_local_source` gains a `mode` keyword argument with
two values:

- **`mode="hide"`** (default; current behaviour). Top-level entries
  are stashed to a sibling temp dir on enter and restored on exit.
  Workspace appears empty during the yield window. Files the agent
  creates survive the restore. **No change from today.**

- **`mode="scaffold"`** (new). Top-level entries are stashed as
  with `hide`, AND 0-byte placeholder files are created at every
  original path under the tracked extensions (see
  `TRUNCATE_EXTENSIONS` below). On exit, the context manager runs
  the 6-step overlay contract (below) so the post-yield workspace
  contains *restored source* plus *agent edits overlaid on top*.

Default stays `hide` for backwards compatibility with existing
`hide_local_source: True` configs and the codeprobe-jf28 use
cases. `mode="scaffold"` is opt-in via the upcoming
`hide_local_source_mode` config field (codeprobe-hcnv).

## TRUNCATE_EXTENSIONS

Mirror the CodeScaleBench list verbatim. Source of truth:
`~/projects/CodeScaleBench/scripts/maintenance/generate_sgonly_dockerfiles.py`
lines 283-326. Reproduced here so codeprobe doesn't depend on the
CSB checkout existing on disk; **if CSB updates its list, this
list must be updated in lockstep** — the truncate surface is a
security boundary, not a convenience filter. A narrower list
opens leak channels (an agent could `cat foo.proto` and bypass
sg-only on any repo that contains a `.proto`).

```python
TRUNCATE_EXTENSIONS = (
    # Python
    "*.py", "*.pyx", "*.pyi",
    # JavaScript / TypeScript
    "*.js", "*.ts", "*.jsx", "*.tsx", "*.mjs", "*.cjs", "*.mts", "*.cts",
    # Go
    "*.go",
    # Java / JVM
    "*.java", "*.kt", "*.scala", "*.groovy", "*.clj",
    # C / C++ (including .cc used by Envoy, gRPC, Chromium, etc.)
    "*.c", "*.cc", "*.cpp", "*.cxx", "*.h", "*.hh", "*.hpp", "*.hxx",
    # Rust
    "*.rs",
    # Ruby
    "*.rb",
    # C# / .NET
    "*.cs", "*.fs",
    # Swift / Objective-C
    "*.swift", "*.m", "*.mm",
    # Web frameworks
    "*.vue", "*.svelte",
    # Shell
    "*.sh", "*.bash", "*.zsh",
    # Lua
    "*.lua",
    # Protobuf / gRPC / IDL
    "*.proto", "*.thrift", "*.avsc", "*.fbs",
    # Config / data (often contains structural info agents can exploit)
    "*.yaml", "*.yml", "*.toml", "*.json", "*.xml", "*.ini", "*.cfg",
    # Documentation (agents can extract architecture info)
    "*.md", "*.rst", "*.txt", "*.adoc",
    # Build files
    "*.cmake", "*.bzl", "*.bazel",
    # SQL
    "*.sql",
    # Erlang / Elixir
    "*.erl", "*.ex", "*.exs",
    # PHP
    "*.php",
    # Perl
    "*.pl", "*.pm",
    # R
    "*.r", "*.R",
)
```

Exclusions when walking the tree to create placeholders:

- `*/.git/*` — version control state must remain readable so
  the agent's `git status`/`git diff` calls work and so codeprobe's
  own pin/restore plumbing continues to function.
- `*/tests/*` — test fixtures are part of the oracle, not the
  source under inspection. CSB's `backup_agent_files` also skips
  `tests/`.
- `*/.codeprobe*` — codeprobe metadata and worktree pool dirs
  (`.codeprobe`, `.codeprobe-worktrees*`, `.codeprobe-source-stash-*`).
- `*/node_modules/*` — vendored code is never agent-authored.

### Threat model scope (out of scope for sg-only)

The truncate surface blocks MCP `read_file`-style direct file
reads. It does NOT block git-plumbing reads. An agent that
runs `git cat-file -p HEAD`, `git show HEAD:src/foo.py`, or
walks `.git/objects/` via `git rev-list` can reconstruct the
full source tree from the kept `.git/` directory.

This is a known and accepted gap, consistent with CSB's
Docker-image variant which also leaves `.git/` intact inside
the container. Closing this gap would require either (a) bare-
init-then-restore plumbing that breaks `git status`/`git diff`
for the agent's own edits during the yield, or (b) stripping
`.git/objects/` and rebuilding a synthetic index — both add
complexity and break the operational invariants the kept `.git/`
provides.

If a future bead needs to harden against git-plumbing reads,
it owns the scope expansion explicitly. The default scaffold
mode does not.

## Stash and manifest layout

The architect review (C1) flagged that putting the manifest inside
the workspace would cause it to be stashed during the yield
window. The design therefore places **everything outside the
workspace**:

```
<workspace>.parent/
├── .codeprobe-source-stash-<uuid>/         # stash dir (same as today)
│   ├── src/                                # ← real source, moved here
│   ├── README.md                           # ← real source, moved here
│   └── manifest.json                       # ← sg-only manifest (NEW)
└── <workspace>/                            # what the agent sees
    ├── .git/                               # kept
    ├── .codeprobe/                         # kept
    ├── src/foo.py                          # 0-byte placeholder
    └── src/bar.go                          # 0-byte placeholder
```

`manifest.json` schema:

```json
{
  "mode": "scaffold",
  "stash_dir": "/abs/path/.codeprobe-source-stash-abc12345",
  "scaffold_paths": [
    "src/foo.py",
    "src/bar.go",
    "README.md"
  ],
  "created_at": "2026-05-11T12:34:56Z"
}
```

- `mode` lets future modes (e.g. partial-scaffold) reuse the same
  manifest shape.
- `stash_dir` is an absolute path so a verifier started from a
  different cwd can still find the source.
- `scaffold_paths` are workspace-relative POSIX paths to every
  placeholder file. The overlay step uses this set to distinguish
  "agent grew this from 0 → N bytes" (overlay back) from "agent
  created a brand new file" (also overlay back) from "agent did
  nothing to this placeholder" (real source wins on restore).
- `created_at` is informational; not used in restore logic.

The manifest does NOT need to be agent-discoverable. It's an
internal codeprobe artifact. The context manager writes it on
enter, reads it inside `__exit__`, and deletes the stash dir
(manifest included) when restore completes.

## __exit__ ordering — codeprobe-specific 6-step contract

The architect review (C2) flagged that CSB's "wrapper sourced
from test.sh" pattern doesn't translate to codeprobe, because
codeprobe's scoring runs **after** the `quarantine_local_source`
context manager has returned. By the time the verifier sees the
workspace, the context manager has already done all the work.

The contract for `mode="scaffold"` on `__exit__`:

1. **Yield window ends.** Agent has finished writing. Workspace
   contains: kept entries (`.git`, `.codeprobe*`), 0-byte
   placeholders the agent left untouched, non-zero placeholders
   the agent grew, brand-new files the agent created at non-tracked
   paths.
2. **Capture agent overlay.** Walk the workspace; collect every
   file that is either (a) in `scaffold_paths` with `size > 0`,
   or (b) NOT in `scaffold_paths` AND NOT in the keep set AND
   not under any excluded directory (see "Overlay filter" below).
   Copy these to `<stash_dir>/__agent_overlay__/<relpath>`.
3. **Remove scaffold placeholders and overlay originals.** Step 2
   *copied* (not moved) the overlay files, so the workspace still
   contains both the tracked-extension placeholders and the
   originals of any non-tracked-extension files captured by the
   overlay. Delete every path in `scaffold_paths` from the
   workspace AND delete the workspace original of every file copied
   into `__agent_overlay__/` in step 2. After this step only the
   keep set (`.git`, `.codeprobe*`) remains.
4. **Restore stashed source.** Move every entry from
   `<stash_dir>/` (except `__agent_overlay__/` and `manifest.json`)
   back to its workspace-relative path. After this step the
   workspace is identical to its pre-quarantine state.
5. **Overlay agent files.** Copy every file under
   `<stash_dir>/__agent_overlay__/<relpath>` back to
   `<workspace>/<relpath>`, overwriting any same-named file in
   the restored source. After this step the workspace contains
   real source plus agent's edits applied on top.
6. **Clean stash.** `shutil.rmtree(stash_dir)`. Manifest dies
   with it.

Scoring runs after the context manager returns. The workspace is
the merged state; `tests/test.sh` operates on it without needing
any sourced wrapper. This is the codeprobe-CSB delta.

**Exception path.** If an exception is raised inside the yield
window, steps 4 and 6 still run (the source must be restored).
Steps 2, 3, 5 are skipped — preserving the agent overlay on
disk under an exception path is a debugging convenience but not
a correctness requirement. The implementer in codeprobe-yw6u
should mirror the `hide`-mode pattern at `isolation.py:330-337`
(restore on `BaseException`).

## Overlay filter rules

Mirrors CSB `backup_agent_files()` (`sgonly_verifier_wrapper.sh`
lines 45-61) plus codeprobe-specific exclusions. A file
qualifies for overlay if and only if:

- It is a regular file (not a directory, not a symlink).
- It has `size > 0`.
- It is NOT under `<workspace>/.git/` (any depth). (`.git` is
  also in `_LOCAL_SOURCE_DEFAULT_KEEP` so it is never stashed —
  this overlay rule is defense-in-depth against direct agent
  writes to `.git/*`.)
- It is NOT under `<workspace>/tests/` (any depth) — CSB excludes
  this; matches. **Writes by the agent under `tests/` are not
  captured in the overlay and are silently discarded on restore.
  This is intentional — `tests/` is the oracle; agents must not
  modify it.**
- It is NOT under `<workspace>/.codeprobe/` (any depth).
- It is NOT under `<workspace>/.claude/` (any depth). CSB
  excludes this too; aligns codeprobe with the same surface so
  agent-tool metadata never leaks into the scored tree.
- It is NOT under `<workspace>/.github/workflows/` (any depth).
  Agent-authored workflow files must not become part of the
  restored repo — any future verifier or post-processing step
  that respects GitHub Actions would otherwise execute attacker-
  controlled code from the overlay.
- It is NOT a top-level entry whose name matches
  `.codeprobe-worktrees*` or `.codeprobe-source-stash-*`.
- It is NOT under any directory containing an `experiment.json`
  (use `_discover_experiment_dirs` from `isolation.py:46-58`).
- It is NOT named `experiment.json` at any depth.

**Deferred for future hardening (codeprobe-sm9f or follow-up):**
top-level build files (`Makefile`, `requirements.txt`,
`pyproject.toml`, `setup.py`, `package.json`, `go.mod`, etc.)
are currently allowed as overlay. The smoke fixture's `test.sh`
runs only `grep`, so this is dormant. The day a verifier
invokes `make`, `pip install`, or `npm ci` against the merged
state, these become an injection vector and need explicit
exclusion. The hcnv wire-up bead should call this out when
choosing tasks for the smoke trial.

Agent-created `answer.txt` files at the workspace root are
overlaid (the codeprobe oracle path for tasks that return a
result file). Same for any non-tracked-extension file the agent
writes — the design accepts a slightly broader overlay surface
than scaffold_paths to support oracle tasks that write JSON
answers, log outputs, etc.

## Handoff contracts to next beads

| Bead | Inherits from this doc | Must implement |
|---|---|---|
| codeprobe-yw6u (.2) | `mode` parameter shape, `TRUNCATE_EXTENSIONS`, stash/manifest layout, step-by-step `__exit__` contract | Add `mode: Literal["hide", "scaffold"] = "hide"` to `quarantine_local_source`; the placeholder-creation pass; manifest write/read; the 6-step overlay logic; flip `SGONLY_SCAFFOLD_AVAILABLE = True` at module scope to unblock `tests/test_isolation_scaffold.py` |
| codeprobe-sm9f (.3) | Overlay filter rules, the 5 fixture cases | Verifier-side wiring so scoring sees the merged tree; integration tests using `tests/fixtures/sdlc_sgonly_smoke/` |
| codeprobe-hcnv (.4) | The whole stack | `ExperimentConfig.hide_local_source_mode: Literal["hide","scaffold"]`; CLI flag; the gascity SDLC smoke trial; results doc |

## Acceptance-criterion-to-file table

| AC | Satisfied by |
|---|---|
| Design doc exists with all required sections | `docs/investigations/codeprobe-2nw2/design.md` (this file) |
| Test fixture exists with all listed files | `tests/fixtures/sdlc_sgonly_smoke/{instruction.md, metadata.json, tests/test.sh, confidence.json, src/math.go}` |
| Test skeleton collectable + skips | `tests/test_isolation_scaffold.py` with module-level `pytest.skip(..., allow_module_level=True)` |
| `pytest tests/test_isolation_scaffold.py` collects but skips | Verified in commit message + close note |
| Merged to main with evidence metadata | Bead close ritual at `CLAUDE.md` §"Bead Close Ritual" |

## Validation walk-through against fixture

Mental walk-through against `tests/fixtures/sdlc_sgonly_smoke/`:

**Step 0 — pre-quarantine state:**
```
workspace/
├── .git/HEAD                (kept)
├── instruction.md
├── metadata.json
├── confidence.json
├── tests/test.sh
└── src/math.go              (contains: "package math\n\n// existing\n")
```

**Step 1 — `quarantine_local_source(workspace, mode="scaffold")`
enters:**
- Stash `instruction.md`, `metadata.json`, `confidence.json`,
  `tests/`, `src/` to `<stash>/`.
- Build `scaffold_paths` by walking the stash matching
  `TRUNCATE_EXTENSIONS`. Excludes `tests/`. Result:
  `["instruction.md", "metadata.json", "confidence.json", "src/math.go"]`.
  (`.md` and `.json` are in the extension list; `.go` is in the
  extension list; `tests/test.sh` is excluded by the
  `*/tests/*` rule.)
- Create 0-byte placeholders at each path.
- Write `<stash>/manifest.json`.

Workspace at yield-time:
```
workspace/
├── .git/HEAD                (kept)
├── instruction.md           (0 bytes)
├── metadata.json            (0 bytes)
├── confidence.json          (0 bytes)
└── src/math.go              (0 bytes)
```

Note: `tests/` is gone (stashed, since the top-level-stash step
moves all non-kept entries, and `tests/` is not in the keep set).
The `tests/` exclusion in scaffold-path generation only means
placeholders aren't created under `tests/`; the directory itself
is still part of the stash. This is by design — the agent should
not have access to `tests/test.sh` content; that's the oracle.

**Step 2 — agent runs.** Suppose the agent writes:
```python
# src/math.go
package math

func add(a int, b int) int {
    return a + b
}
```
to `src/math.go` (turning the placeholder from 0 → N bytes), and
also writes `answer.txt = "done"` at the workspace root.

**Step 3 — `__exit__` runs the 6-step contract:**

1. Yield ends.
2. Capture: `src/math.go` (in `scaffold_paths`, size > 0) AND
   `answer.txt` (not in `scaffold_paths`, not in keep set, not
   excluded) both copy to `<stash>/__agent_overlay__/`.
3. Remove placeholders: `instruction.md`, `metadata.json`,
   `confidence.json`, `src/math.go` (and the workspace-root
   copy of `answer.txt`) all deleted from workspace.
4. Restore stash: `instruction.md`, `metadata.json`,
   `confidence.json`, `tests/`, `src/` all move back from
   `<stash>/`. `src/math.go` now contains
   `"package math\n\n// existing\n"` (original).
5. Overlay: `src/math.go` is overwritten with the agent's
   version (the one with `func add`); `answer.txt` is created
   at workspace root from the overlay.
6. `shutil.rmtree(stash_dir)`.

**Step 4 — scoring:** runs `bash tests/test.sh`. The test
greps `func add` from `src/math.go`. Since the overlay step
copied the agent's version on top, the grep succeeds. Test
exits 0 → reward 1.0.

This walk-through is the **expected behaviour** that
`tests/test_isolation_scaffold.py` codifies via the 5 fixture
cases (codeprobe-sm9f writes the actual assertions).

## ZFC compliance note

The overlay filter is structural: file-system metadata (size,
path-prefix membership) comparison against a manifest captured at
context-manager entry. No semantic judgment about file *content*
is made anywhere in the algorithm. This is a justified exception
under `CLAUDE.md` §ZFC Compliance ("mechanical comparison, not
semantic judgment") and should be listed there when
codeprobe-yw6u lands the implementation.

The `TRUNCATE_EXTENSIONS` allowlist is a security boundary, not a
classification heuristic; updating it is a policy change, not a
threshold tweak.

## Open question deferred to codeprobe-yw6u

- **Empty-directory scaffold paths.** Should
  `scaffold_paths` include directories that get emptied by the
  truncation (e.g. `src/` containing only `.py` files all
  truncated), or only individual file paths? Recommendation:
  individual file paths only; let directory existence follow
  from the file paths' parents. The implementer can revisit if
  there's a test that exercises an "agent created `src/new.go`
  in a directory that contained only truncated files" case.
