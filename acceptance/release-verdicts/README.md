# Release verdict evidence

This directory is populated by the pre-tag release check:

```bash
uv run python scripts/pre_tag_check.py \
  --export-release-evidence acceptance/release-verdicts
```

Commit the generated `manifest.json`, `verdict-previous.json`, and
`verdict-latest.json` before creating the release tag. The tag publication
workflow validates the manifest version and hashes, passes both verdicts to
`ReleaseGate.check_ready()`, and refuses to stage or publish when the evidence
is absent, altered, or not ready.
