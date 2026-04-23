# Enterprise Discovery

> Phase 0 artifacts for the *Enterprise Repo Benchmark Parity* PRD
> (`docs/prd/prd_enterprise_repo_benchmark_parity.md`). No Phase 1 code lands
> until three sanitized partner artifacts exist under this directory.

## Why this directory exists

The PRD's first draft went through a six-lens premortem (April 2026) and every
lens returned Critical × High. The single most common failure narrative across
the lenses was **"we built the right thing for OSS repos and the wrong thing
for the enterprise repos we actually need to serve."** Phase 0 Enterprise
Discovery is the remediation: no Phase 1 implementation work begins until at
least three enterprise partners have contributed a sanitized discovery
artifact to this directory.

In practice that means:

- Phase 1 acceptance criteria are **scoped by partner interview**, not
  by author imagination. If zero partners care about feature X, X doesn't ship
  in Phase 1.
- The taxonomy of task types (`dependency_upgrade`, `framework_migration`,
  `config_surgery`, `runbook_execution`, `observability_change`,
  `compliance_patch`, …) is **derived from partner interviews**, not the
  CodeScaleBench / EnterpriseBench defaults.
- The decision-maker artifact surface (Datadog vs Sigma vs Sheets vs
  internal eval dashboard) is **selected by partner preference**, not by
  what's convenient to implement.

## Required artifacts

To unblock Phase 1, at least three sanitized partner files must be committed
here:

| Filename | Source | Owner |
| --- | --- | --- |
| `<partner-slug>.md` | Using `TEMPLATE.md` — interview notes, taxonomy mapping, artifact preferences, security posture | Discovery interviewer |
| `dogfood-<partner-slug>.md` | `codeprobe mine` run against one of the partner's repos with current (pre-Phase-1) code, documenting every failure | Discovery interviewer + partner staff engineer |
| `taxonomy-delta.md` | Aggregated list of new task types surfaced across the three partner interviews | Project maintainer, once D1 complete |

`TEMPLATE.md` is the schema for partner files. `INTERVIEW_GUIDE.md` is the
90-minute script the interviewer runs. Both are versioned — if you change
the schema or the script, bump the header date and add a CHANGELOG entry.

## Process

1. **Identify partner.** A candidate partner is an organisation whose
   engineers would benefit from a codeprobe-shaped benchmark *and* who can
   spare 90 minutes of a staff engineer's time. Exclude partners who can
   only contribute under terms that forbid the sanitized artifact ever
   reaching this directory — those partners can still be served by
   codeprobe, but their interview can't gate Phase 1.
2. **Schedule the 90-minute interview.** Use `INTERVIEW_GUIDE.md` as the
   script. Do **not** improvise — the structure is there so the three
   partner files are comparable against each other in `taxonomy-delta.md`.
3. **Run the dogfood mine.** Execute current `codeprobe mine` against the
   repo the partner contributes (or a sanitized mirror of it). Capture
   every failure mode, every crash, every silently-wrong output. Land
   this as `dogfood-<partner-slug>.md`.
4. **Partner-review the sanitization.** Before the partner file merges,
   the partner reviews the sanitized draft and signs off in writing that
   the redactions are sufficient. Keep a record of the sign-off (not in
   this repo — in the partner's preferred channel, ideally with a hash of
   the committed file so tampering is detectable).
5. **Aggregate.** Once ≥3 partner files exist, land `taxonomy-delta.md`
   summarising the new task types and the decision-maker artifact
   preferences. This file closes Phase 0.

## Sanitization checklist

Every partner file lands with:

- [ ] No real repository names. Use `<partner-repo-1>`, `<partner-repo-2>`.
- [ ] No real engineer names. Use `<staff-engineer-1>`, `<platform-lead>`.
- [ ] No real Slack channel names, Jira project keys, Datadog monitor names,
      or internal URLs.
- [ ] No cloud project IDs, AWS account numbers, GCP project IDs, or
      Azure subscription IDs.
- [ ] Partner has reviewed the sanitized draft and confirmed in writing
      that the redactions are sufficient.
- [ ] Concrete numbers (repo size, PR frequency, team size) are rounded
      to orders of magnitude where the partner requests it.
- [ ] Any tool names that would uniquely identify the partner (internal
      build systems, bespoke code-search tools) are replaced with
      generic descriptions.

## What Phase 1 does with these artifacts

Three concrete Phase 1 requirements are modified per partner input:

- **R1** (MCP preamble widening) — the capability set encoded in
  `preambles/*.md` is derived from the partners' real MCP tool inventories,
  not a Sourcegraph-specific string table.
- **R3-new** (enterprise task types) — the new task-type generators added
  under `mining/` are picked from `taxonomy-delta.md`, not from a
  pre-authored list.
- **R4** (expected-tool-benefit field) — the `expected_tool_benefit` values
  and rationale prompts are parameterised on the partner's chosen tooling
  environment, not on an assumed Sourcegraph deployment.

The PRD's Exit Criteria explicitly require that **at least one Phase 1 or
Phase 2 acceptance criterion has been modified based on a finding in these
artifacts.** If every Phase 1 criterion survives discovery unchanged, that's
strong evidence we've done discovery wrong — either the interviews were too
narrow or the criteria were written with partners in mind already.

## Tooling

- Transcription: use the partner's preferred meeting platform's built-in
  transcription when the partner consents. Otherwise take written notes
  and share a summary for confirmation.
- Redaction: a simple pass is fine for a first draft — final redaction
  happens with the partner present.
- Storage: sanitized markdown files live here. Raw transcripts and
  partner-sensitive source material never enter this repository. Store
  them in the partner's preferred secure channel or a project-private
  vault with access logged.

## Open questions

These are the questions the discovery process itself has not yet answered
— carry them into the first three interviews explicitly:

- **Q-D1**: Do partners treat "benchmark" as a noun they already own,
  or a category they will ask us to define? The answer changes how R11
  calibration is presented.
- **Q-D2**: Is the decision-maker artifact surface converging (all
  partners surface to Datadog) or diverging (each partner has a
  bespoke internal dashboard)? R19 gets scoped from the answer.
- **Q-D3**: How many of the partners have a security / legal review
  function that would need to sign off on codeprobe's trust boundary
  before any pilot? Feeds the sequencing of R14 and R_W.

## Status

| Partner | Discovery file | Dogfood file | Status |
| --- | --- | --- | --- |
| `<partner-1-slug>` | pending | pending | scheduled |
| `<partner-2-slug>` | pending | pending | outreach sent |
| `<partner-3-slug>` | pending | pending | identifying |

Update this table as partner files land. Phase 1 implementation work begins
when all three rows have both files committed.
