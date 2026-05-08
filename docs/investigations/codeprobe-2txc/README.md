# codeprobe-2txc — preamble-tune effect rerun

**Status:** complete
**Branch:** `feature/codeprobe-x7p3-validate-unified-contract`
**Predecessors:** codeprobe-3oms, codeprobe-mcn7, codeprobe-ttwq, codeprobe-ovz2
**Follow-up filed:** codeprobe-riad — refine oracle_checks branch + audit Sourcegraph index

## What's here

- `eval_writeup.md` — full writeup with three contrasts, verdict, and follow-up
- `analyze.py` — aggregation + paired-t for oracle_checks (and SDLC reference data)
- `per_trial.json` — flat trials including new oracle_checks tuned-preamble + reference data
- `per_family_summary.json` — config / per-task / family-level summaries + contrasts
- `aggregate.json` — distilled key-contrast envelope
- `logs/run.{stdout,stderr}.log` — codeprobe run output for the new oracle_checks trials

## Critical setup discovery

While preparing this rerun, we discovered that `mcn7`'s `with-sourcegraph` runs
already used the SDLC-tuned preamble — `preamble.py` was modified (uncommitted)
~10 minutes before mcn7 launched its with-sg trials. As a result, mcn7's
`with-sourcegraph` is effectively `with-sg-tuned-preamble` for SDLC.

To avoid duplicating mcn7's SDLC work, this bead runs only the missing data
point: 15 `oracle_checks` trials with the tuned preamble (5 tasks × N=3). The
SDLC-tuned-vs-baseline contrast comes from mcn7 directly, and the SDLC
preamble-effect contrast falls back on 3oms's N=1 default-preamble reference.

See `eval_writeup.md` for full reasoning.
