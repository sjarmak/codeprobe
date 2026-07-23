# Release Runbook

How a codeprobe release actually ships, and what gates it.

## Preconditions

Before tagging, confirm all three:

1. **Acceptance verdicts are green.** `ReleaseGate.check_ready(verdict_paths)`
   (`acceptance/release.py`) returns `True` only when the last two
   `verdict.json` files from the acceptance loop are both `status ==
   "EVALUATED"` and `all_pass is True`. This check is local-only — CI has no
   view of acceptance-loop verdict history — so it runs as part of the
   release skill, before anything is tagged.
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
test  →  gate  →  publish
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
  Only then does `scripts/release_gate.py` run, as a separate smoke test:
  it invokes `ReleaseGate.build_and_stage()`, which rebuilds the wheel
  again, installs it into a throwaway venv, runs `codeprobe --version`, and
  exercises five structural acceptance criteria against the freshly-staged
  install. That rebuild is deliberately sequenced *after* the artifact
  upload: `python -m build` wheel output is not byte-reproducible between
  invocations even against an unchanged source tree (differing zip-entry
  timestamps/metadata), so if `release_gate.py` ran before the upload step,
  the artifact would silently capture its unchecked rebuild instead of the
  wheel `check_release_artifacts.py` actually validated.
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
