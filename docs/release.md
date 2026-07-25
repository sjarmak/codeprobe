# Release Runbook

How a codeprobe release actually ships, and what gates it.

## Preconditions

Before tagging, confirm all three:

1. **Acceptance verdicts are green.** Produce the verdict history by running
   the acceptance loop, then check it:

   ```bash
   uv run python scripts/acceptance_loop.py --eval-mode full --iterations 2 \
       --target-repo <a real repo> --producer-agent claude
   uv run python scripts/pre_tag_check.py \
       --export-release-evidence acceptance/release-verdicts
   ```

   `scripts/acceptance_loop.py` compiles the criteria manifest into Test
   Agent actions, executes them in a fresh workspace, verifies, and writes
   `verdict-NNNN.json` files into `acceptance/verdict-history/` (gitignored,
   durable — to reset the loop, delete that whole directory including
   `converge.db`). `scripts/pre_tag_check.py` runs
   `ReleaseGate.check_ready(verdict_paths)` (`acceptance/release.py`) over
   the two newest verdicts — both must be `status == "EVALUATED"` with
   `all_pass is True` — plus preconditions 2 and 3 below, and exits nonzero
   with the fixing command when anything is not ready. The export option runs
   only after every check passes. It copies those two verdicts into
   `acceptance/release-verdicts/` and writes a manifest bound to the release
   version and the verdict content hashes. Commit all three generated JSON
   files before creating the tag. This tracked handoff gives the fresh tag
   workflow the exact real verdict history that passed locally.

   **Mode caveat (binding for 0.13.0 and later):** a default-mode green is
   NOT release evidence for the mode-gated tiers. In `--eval-mode default`,
   every criterion carrying `eval_mode_required = "full"` is excluded from
   the evaluated denominator, so the behavioral and statistical tiers can
   report 100% while evaluating little or nothing. Both verdicts feeding the
   release decision must come from
   `--eval-mode full` (real agent runs, real spend) before a tag is
   created; `pre_tag_check.py` requires both verdicts to be full-mode and
   enforces this mechanically via the `eval_mode` field recorded in each
   verdict.

   **Producer agent (binding for 0.13.0 and later):** `--eval-mode full`
   requires `--producer-agent` — no silent default. The loop runs a real
   `codeprobe mine` + `codeprobe run --agent <producer-agent>` against
   `--target-repo`, aggregates the per-arm results into
   `.codeprobe/results.json`, and only then can the statistical criteria
   (`SILENT-RUN-RESULTS-002`, `TELEM-COST-SOURCE-001`, `TELEM-COST-USD-002`)
   evaluate instead of skipping. The chosen agent is stamped into every
   full-mode verdict as `producer_agent`. **The two full-mode greens gating
   0.13.0 MUST come from a REAL agent producer (e.g. `--producer-agent
   claude`), never the `e2e-stub`.** `e2e-stub` is a zero-budget wiring
   check only: its telemetry is honest-but-fake (`cost_source =
   "unavailable"`, `cost_usd = 0.0`), which passes the `TELEM-*` criteria
   without any genuine cost signal — so a verdict whose `producer_agent` is
   `e2e-stub` is NOT release evidence. Before tagging, confirm both feeding
   verdicts record a real `producer_agent`.

   The exact 0.13.0 gate command an operator runs (real agent, real spend):

   ```bash
   uv run python scripts/acceptance_loop.py --eval-mode full --iterations 2 \
       --target-repo <a real git repo> --producer-agent claude
   uv run python scripts/pre_tag_check.py \
       --export-release-evidence acceptance/release-verdicts
   git add acceptance/release-verdicts
   git commit -m "chore: record release verdict evidence"
   ```
2. **`CHANGELOG.md` has an entry for the version being released.** A `##
   <version>` heading; see the `Unreleased` section for the running list of
   changes since the last release. `scripts/check_release_artifacts.py`
   enforces this mechanically once a tag is pushed, but confirm before
   tagging, not after.
3. **The version is bumped.** `ReleaseGate.bump_version(bump_type)`
   increments `[project].version` in `pyproject.toml` (`"major"` /
   `"minor"` / `"patch"`, default `"patch"`) and returns the new version
   string. Commit this alongside the `CHANGELOG.md` edit that moves the
   `Unreleased` content under the new version heading.

`ReleaseGate.prepare_tag(version)` is a pure helper that returns `f"v
{version}"` — it does not create or push the tag. The release skill decides
when (and whether) to actually do that.

## Tag-push flow

```bash
git tag v<version>
git push origin v<version>
```

Pushing the tag triggers `.github/workflows/publish.yml`:

```
test ───────────┐
                ├→ gate → publish
e2e-self-serve ─┘
```

- **`test`** — the normal pytest matrix (3.11/3.12/3.13), same as CI.
- **`gate`** — builds `dist/` exactly once (`python -m build`), runs `twine
  check dist/*`, then `scripts/check_release_artifacts.py dist/ --version
  <tag-version>` — structural checks: exactly one wheel + one sdist; both
  contain exactly the five packaged skills and nothing else (this is the
  check that would have caught the 0.11.0 leak, see below); wheel/sdist
  filename versions match `pyproject.toml`; `CHANGELOG.md` has a heading for
  that version. **Immediately after that check passes**, `dist/` is uploaded
  as the `release-dist` workflow artifact — before the next step runs.
  Only then does `scripts/release_gate.py` run the complete acceptance gate.
  It loads `acceptance/release-verdicts/manifest.json`, rejects a missing,
  stale-version, or hash-mismatched evidence set, and passes the two ordered
  verdicts directly to `ReleaseGate.check_ready()`. Staging is unreachable
  unless both verdicts are `EVALUATED` with `all_pass is True`. Once ready,
  the same `ReleaseGate` instance runs `build_and_stage()`: it rebuilds the
  wheel, installs it into a throwaway venv, runs `codeprobe --version`, and
  exercises five structural acceptance criteria against the freshly-staged
  install. Every staging field must be true. That rebuild is deliberately
  sequenced *after* the artifact upload: `python -m build` wheel output is
  not byte-reproducible between invocations even against an unchanged source
  tree (differing zip-entry timestamps/metadata), so if `release_gate.py`
  ran before the upload step, the artifact would silently capture its
  unchecked rebuild instead of the wheel `check_release_artifacts.py`
  actually validated.
- **`publish`** — downloads `release-dist` and runs `twine upload dist/*`.
  It never rebuilds. The bytes `gate` uploaded (immediately after checking
  them, before any later rebuild) are the exact bytes published — this is
  deliberate: rebuilding between check and publish would mean the gate
  proved nothing about what customers actually receive.

If `gate` fails, the tag exists but nothing is published. Delete the tag,
fix the issue, and re-tag once `pyproject.toml`'s version still matches
what you intend to ship (re-tagging the same version after a failed publish
is safe — PyPI rejects a re-upload of an already-published version, and
nothing was uploaded).

## PyPI yank runbook: the leaked 0.11.0 sdist

**Background.** The 0.11.0 sdist (published 2026-05-11) contains 15
unrelated skill directories — `.claude/skills/*/SKILL.md`, including
deprecated pre-v0.6.0 names like `mine-tasks` and `run-eval` — because
`MANIFEST.in` had a `recursive-include .claude/skills SKILL.md` rule that
swept whatever machine-local agent skills the build host happened to have
into the sdist. The rule has since been removed; `MANIFEST.in` now
explicitly documents why it must never come back (see the comment in that
file). The **wheel** for 0.11.0 is clean — skills are declared per-package
in `[tool.setuptools.package-data]`, not via a recursive glob, so the wheel
was never affected. `scripts/check_release_artifacts.py` (gating every
release from 0.12.0 onward) would have caught this before publish.

**Action: yank the 0.11.0 sdist only. Do not yank the wheel.** Yanking on
PyPI hides a release from default dependency resolution
(`pip install codeprobe` will not select a yanked version) without deleting
it — existing pins (`codeprobe==0.11.0`) still resolve, so this doesn't
break anyone who already depends on 0.11.0. PyPI yanks apply per-release,
not per-file, so this specifically means: yank release 0.11.0 in a way that
only removes the sdist, keeping the wheel installable. In practice this
means re-uploading a corrected 0.11.0 sdist is not possible (PyPI never
allows overwriting a published file), so the actual mechanism is:

1. Yank the 0.11.0 **release** on PyPI (web UI: project page → Release
   history → 0.11.0 → Options → Yank, or `twine`/`pypi` API equivalent),
   with a yank reason citing this runbook (e.g. "sdist leaked unrelated
   internal skill files; wheel is unaffected, prefer the wheel or 0.12.0+").
2. Because PyPI yanks the whole release rather than a single file, this
   also yanks the (clean) wheel. That's an acceptable tradeoff here:
   `pip install codeprobe==0.11.0` still works for anyone pinned to it
   (yanked releases remain installable when explicitly pinned), and
   unpinned installs move to 0.12.0+ once it ships — which is the outcome
   we want regardless of which artifact within 0.11.0 was the problem.
3. Record the yank (date, who performed it, and the PyPI-assigned reason
   text) as a note under the `0.11.0` entry in `CHANGELOG.md`.

**This is a maintainer action requiring explicit execution/approval** — this
runbook documents the "what" and "why"; nothing in this repository performs
the yank automatically.
