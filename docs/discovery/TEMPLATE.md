# Discovery — `<partner-slug>`

> Sanitized discovery artifact, per Phase 0 D1 of the *Enterprise Repo
> Benchmark Parity* PRD. Copy this file to `docs/discovery/<partner-slug>.md`
> before filling it in. Partner reviewed and approved sanitization on
> `<YYYY-MM-DD>`.

## Metadata

| Field | Value |
| --- | --- |
| Partner slug | `<partner-slug>` |
| Partner profile | `<industry / size-band / stack summary>` |
| Interviewer | `<your-name>` |
| Interviewee role | `<staff engineer / platform lead / …>` |
| Interview date | `<YYYY-MM-DD>` |
| Duration | `<minutes>` |
| Sanitization reviewed by | `<partner-reviewer>` on `<YYYY-MM-DD>` |
| Consent record | `<link or hash, out-of-repo>` |
| Contributed repo | `<sanitized description — language, size band, team size>` |
| Taxonomy mapping | see §Taxonomy Mapping |
| Related dogfood file | `dogfood-<partner-slug>.md` |

## Evaluation Practice

Describe how the partner currently evaluates AI coding agents. Cover:

- What tools / agents they have in production or pilot today (Copilot,
  Cursor, Claude Code, internal wrappers, home-grown rubric reviewers).
- How they measure success. Time-on-task? Acceptance rate in the IDE?
  Review-comment delta? None of the above?
- Who owns the evaluation function in the org. Platform team? DevEx?
  An interested SRE? Nobody officially?
- Cadence — one-off pilots, continuous monitoring, quarterly bakeoffs.
- What they wish they could measure but can't today. This is the richest
  prompt — the gap between "what we measure" and "what we want to
  measure" is where codeprobe either fits or doesn't.
- Any past pilots that ended. If they ended because "the numbers didn't
  mean anything," that's the critical signal. If they ended because of
  budget, codeprobe is competing against "do nothing."

### Direct quotes

Capture 2–4 short verbatim quotes (with permission, redacted). Quotes
travel better than paraphrase when this file is read six months later
by someone who wasn't in the interview.

## Decision-Maker Artifacts

List every artifact the partner's decision-makers (VP Eng, platform lead,
procurement, FinOps, security) consume when making a tooling decision.

For each artifact, capture:

- **Name / tool** — Datadog dashboard, Sigma workbook, Looker explore,
  Google Sheets, OKR doc, SOC-2 control report, internal eval portal.
- **Cadence** — weekly, monthly, per-decision.
- **Freshness requirement** — is yesterday's data ok? Real-time?
- **Who produces it today** — platform team, DevEx, manual extract.
- **Where codeprobe output would need to land** for this artifact to
  exist. (If codeprobe only produces `browse.html`, does that reach
  this decision-maker? If not, what adapter closes the gap?)

This section directly feeds R19 (observability-adapter exports). A
"JSON + adapter matrix" is only useful if we know which adapters to ship
in Phase 4. This is where we find out.

### Procurement / security checkpoints

- Does the partner have a procurement checklist that gates which tools
  the engineers can evaluate?
- Does the security team maintain a list of approved / forbidden
  dataflows? Where does agent trace persistence land on that list?
- Is there a data-residency constraint (EU-only, US-only, customer
  region)?

## Benchmark Definition

How does this partner define "benchmark" internally?

Candidate framings:

- A fixed test suite (CSB-style, EB-style).
- A continuous eval on production PR traffic.
- A rubric applied by senior engineers to sampled agent output.
- A dashboard aggregating incidental metrics (acceptance rate,
  reversion rate, comment-count delta).
- "Benchmark" is not a noun in their vocabulary — they have no
  equivalent.

The answer determines how `codeprobe interpret` output needs to be
presented. If "benchmark" means "fixed suite" the `summary.json +
ranking.json` shape is native; if "benchmark" means "continuous
dashboard" the R19 Datadog exporter path is closer.

### Reward / scoring preference

Capture the partner's preference between:

- **Binary** (pass/fail on test.sh) — simplest, most defensible.
- **Continuous** (weighted checklist, partial credit) — matches
  EB practice, harder to explain to non-engineers.
- **Rubric** (model-scored dimensions) — hardest to defend under
  procurement scrutiny.
- **Composite** — some combination with documented weights.

Note any constraints from the partner's compliance posture —
model-scored reward may be blocked outright by some security reviews.

## Taxonomy Mapping

For each task type from the CSB / EB taxonomy, mark what fraction of
the partner's engineering work it covers. Fractions are rough — "nearly
everything", "some", "rare", "never" are fine; percentages are ok if the
partner offers them unprompted.

| Task type | Partner coverage | Notes |
| --- | --- | --- |
| `crossrepo` | `<fraction>` | |
| `understand` | `<fraction>` | |
| `refactor` | `<fraction>` | |
| `security` | `<fraction>` | |
| `feature` | `<fraction>` | |
| `debug` | `<fraction>` | |
| `fix` | `<fraction>` | |
| `test` | `<fraction>` | |
| `document` | `<fraction>` | |

### Gaps (feeds R3-new)

Which task types *don't* fit the CSB/EB list but describe the partner's
real work? Likely candidates surfaced by the premortem:

- `dependency_upgrade`
- `framework_migration`
- `config_surgery`
- `runbook_execution`
- `observability_change`
- `compliance_patch`

For each gap, capture:

- A one-sentence description of the task type.
- A rough frequency ("we run ~10 of these a quarter").
- Whether ground truth exists today (PR trail, runbook wiki, ticket
  history) or has to be reconstructed.

The aggregate of these gaps across all three partners becomes
`taxonomy-delta.md` and drives R3-new selection.

## Non-PR Artifacts

Not all engineering narrative lives in merged PRs. This section inventories
the artifacts the partner uses that carry engineering rationale but don't
surface in `gh pr list`.

- **RFC documents** — where are they? Markdown in a repo? Notion?
  Confluence? Google Docs? Is there a standard template?
- **Jira / Linear / Shortcut tickets** — what shape carries the rationale?
  Epic description? Ticket body? Comments?
- **Slack / chat threads** — which channels? How are they archived?
  Is there an expectation they're part of the engineering record?
- **CODEOWNERS + churn signal** — does the partner already use
  ownership / churn as a review signal?
- **CI failure history** — accessible? For how far back?
- **Runbooks / incident reports** — where do they live, and who
  updates them?
- **Design-review meeting notes** — do they exist as a durable artifact,
  or are they ephemeral?

This feeds R9 directly — the `NarrativeAdapter` set we ship in Phase 2
is chosen from the union of these artifacts across the three partner
interviews. The partner's ranking of "which of these adapters would
help us most" is a first-class input.

### Adapter shortlist for this partner

Based on what the partner has and what they'd use, which adapters
would unlock the most ground truth for them? Rank by partner preference:

1. `<adapter-1>`
2. `<adapter-2>`
3. `<adapter-3>`

## Security Trust Boundary

This is the section the partner's security team would read. Assume
hostile scrutiny.

### Data that flows out of the partner environment

- What source bytes does codeprobe read? (code, PR metadata, issue
  bodies, CI logs, commits.)
- What source bytes does codeprobe *persist* after reading? (resolved
  prompts, traces, agent outputs, scoring details.)
- What goes to the LLM API? For each LLM backend (Anthropic, Bedrock,
  Vertex, Azure OpenAI, on-prem vLLM): is it approved by the partner's
  security function? Under what terms?

### Redaction posture

- Is the partner comfortable with `--redact=hashes-only` as the default
  for any shared snapshot?
- Would the partner ever share `--redact=contents` or
  `--redact=secrets` output externally? If so, with whom?
- Do they have a pre-existing secret-scan tool they'd require us to
  honor (gitleaks, trufflehog, their own)?

### Tenant isolation

- Per INV2, state is namespaced under `{tenant_id}/{repo_hash}`. What
  tenant identifier would the partner issue? A team name? A signed
  token? Their org short-code?
- Does the partner want codeprobe to refuse cross-tenant reads at the
  CLI level, or is that a nice-to-have?

### Execution posture

- Per INV4, agent execution is containerized by default against a
  read-only bind mount. Does the partner's environment support Docker?
  Podman? Neither — do they need a bwrap / nsjail path?
- Is outbound network from the container restricted? To what hosts?

### Approval path

- Who in the partner org would sign off on codeprobe for a pilot?
  Security? Legal? Platform lead?
- How long does that approval typically take?
- What artifacts would the approver need? A one-pager? A trust-boundary
  diagram? A SOC-2 control matrix? A copy of this file?

### Known blockers

List anything the partner flagged as an outright blocker. Examples:

- "We can't send source bytes to any non-approved LLM backend."
- "Any durable agent trace containing source has to live on our
  infrastructure."
- "OAuth-only; we don't issue PATs."
- "Airgap required for the production codebase; the pilot has to run
  in our internal cluster."

---

## Action Items

Concrete next steps this interview produced:

- [ ] `<action 1>`
- [ ] `<action 2>`
- [ ] `<action 3>`

Each action should map either to a bead in the `codeprobe-ssf` epic or
to a line item in the next PRD revision. If an action has neither, it
will be forgotten — pick one.

## Followups

Questions the partner raised that we couldn't answer in the room:

- `<question 1>`
- `<question 2>`

Respond in writing within one week.
