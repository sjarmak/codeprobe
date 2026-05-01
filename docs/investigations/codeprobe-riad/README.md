# codeprobe-riad

oc_004 was failing on the `names_toml_tag` criterion 0/3 across both
default and tuned `sourcegraph` preambles (codeprobe-ttwq, codeprobe-2txc).
This bead refines the preamble with a `verify-via-local-Grep before denying
existence` rule, audits + refreshes the stale Sourcegraph index that
caused the false-negative cascade, and reruns oc_004 N=3 to confirm the
fix.

**Result:** all three trials now score 1.0, including `names_toml_tag` 3/3.

See [eval_writeup.md](./eval_writeup.md) for the full analysis.
