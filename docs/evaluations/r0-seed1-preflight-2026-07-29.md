# R0 seed-1 pair-yield preflight — 2026-07-29

This preflight ran one fresh seed for the five repositories retained by the
previous R0 campaign, using the current codeprobe scorer and the canonical AOA
measurement-observation contract. It intentionally stopped before `K=3`.

## Result

| Repository | Admitted | Candidates | Pair yield | 0.80 floor |
| --- | ---: | ---: | ---: | --- |
| marshmallow | 6 | 7 | 0.857 | pass |
| isort | 11 | 14 | 0.786 | fail |
| gunicorn | 7 | 16 | 0.438 | fail |
| requests | 6 | 14 | 0.429 | fail |
| rich | 10 | 19 | 0.526 | fail |

The 210 codeprobe trials had two bounded infrastructure failures, both on the
same long-running gunicorn import-chain task. Missing or malformed
agent-authored `answer.json` files remained scored incorrect results and did
not cause pair exclusion.

Of 30 excluded candidates, 29 lacked a repository file-read/write footprint.
The remaining requests task had an oracle chain that did not resolve against
the pinned SCIP file universe. Search-only `Grep` navigation was the dominant
empty-footprint shape: its query proves that a search happened, but not which
repository file the agent opened.

## Decision

Do not expand to `K=3`. Comprehension-task instructions must require at least
one relevant repository source-file read before `answer.json` is written.
This is symmetric across arms and agents and does not weaken AOA admission.

A one-task live canary on the previously excluded requests
`dependency_analysis` task verified the changed instruction: its trace
contained `Grep`, then `Read` of `src/requests/hooks.py`, then `Write` of
`answer.json`; it scored 0.86 with no infrastructure failure.

The local evidence bundle is under
`runs/r0-preflight-current/out/`; runs are intentionally ignored because they
contain large generated worktrees and transcripts.
