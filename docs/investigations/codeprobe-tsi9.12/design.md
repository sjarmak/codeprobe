# Decision codeprobe-tsi9.12: Production outcome identities and cohort validity rules

- **Bead:** `codeprobe-tsi9.12` (decision, P0), parent epic `codeprobe-tsi9.5`
- **Spec:** [Repository Intelligence and Proof Framework PRD](../../strategy/repository_intelligence_proof_framework.md) §Bet 4
- **Executable form:** `src/codeprobe/outcomes/` (types + reference logic), `tests/outcomes/` (measured evidence)
- **Status:** selected; partner-corpus calibration pending (see §7)

## 1. Context

Bet 4 (Production Outcome Bridge) joins an agent trial to what happens to its
change after merge: acceptance, survival, revert, corrective churn, review
burden, incidents, and human takeover. The bridge is only as trustworthy as its
weakest join. Before any ingestion (epic `codeprobe-tsi9.5`) can start, four
things must be fixed: how a trial is identified in the production record, how
confident that identity is, what each outcome means with its observation window
and censoring, and the validity rules for the matched-cohort study the PRD calls
the first product. This document records those selections and the measured
linkage behaviour that justifies them.

## 2. Identity precedence and confidence

**Selected precedence (strongest first):**

| Tier | Signal | Match rule | Confidence |
|------|--------|-----------|------------|
| 0 | `run_marker` | An explicit marker CodeProbe embeds in the requested patch is found in the change's commit message / PR body | `CONFIRMED` |
| 1 | `patch_digest` | Content hash of the produced diff equals the change's digest | `HIGH` |
| 1 | `commit_sha` | Direct commit SHA equality (connector-resolved) | `HIGH` |
| 2 | `pr_number` | PR-number equality only | `MEDIUM` |
| 3 | `heuristic` | Pre-labelled author/time/path proximity supplied by a connector | `LOW` |

Resolution proceeds tier by tier, strongest first. Signals sharing a tier
(`patch_digest` and `commit_sha` are both HIGH) are evaluated **jointly**: the
resolver collects every change matched by *any* signal of the tier, and a
*single distinct* matched change links. Two or more distinct changes matched
within the tier — whether via the same signal or two different equal-confidence
signals — is a genuine collision and is refused as `ambiguous` rather than
resolved to whichever signal the code checks first. (A naive strongest-first
walk that ranked `patch_digest` strictly above `commit_sha` would silently link
a digest-decoy while a commit-SHA match to the real change went unexamined; the
tier model closes that gap.) Callers pass a `min_confidence` floor so an outcome
that needs line-level attribution can require `HIGH` identity.

**Repository scoping (precondition, enforced).** Every identity comparison is
gated on `repo` equality. PR numbers restart at 1 per repository and short
hashes are not globally unique, so comparing signals across repositories would
manufacture confident-but-wrong links — the same failure class this decision
disqualifies bare digests for. `TrialFingerprint` and `ChangeRecord` both carry
a required `repo`; the resolver never compares across it. Multi-tenant scoping
(`tenant_id`) is the same mechanism extended, and binds to the retention policy
in `codeprobe-tsi9.11`.

**Signal entropy (precondition, connector-enforced).** The HIGH/CONFIRMED tiers
assume collision-resistant inputs: `patch_digest` must be a full-length SHA-256
of the normalised diff (no truncation) and `run_marker` must be unique per trial
(e.g. a UUID4 embedded in the requested patch). A truncated hash or low-entropy
marker would inherit a tier it has not earned and silently inflate the real
false-link rate above §3's measured numbers. This is a construction precondition
the ingestion connector (epic `codeprobe-tsi9.5`) validates at its boundary; the
contract type documents it rather than re-validating post hoc.

This is encoded in `src/codeprobe/outcomes/identity.py`. Matching is exact
equality on structural identifiers only; the fuzzy proximity heuristic is not
synthesised in the resolver but supplied pre-labelled by a connector, keeping
semantic judgment out of the deterministic core (ZFC, §6).

### Why run-marker is primary, not patch-digest

The intuitive primary key is the patch content digest, which needs no
instrumentation. The prototype shows why that is not enough. In the fixture,
two unrelated changes share a digest (`D6`), and one change collides with a
trial that did not produce it (`DX`). The digest resolver links the second case
*confidently and wrongly*. A `HIGH` confidence floor does **not** remove that
false link, because the collision is itself a `HIGH`-tier match; the floor only
drops the correct `MEDIUM`/`LOW` links and lowers recall. The `CONFIRMED` tier,
driven by an embedded run marker, carries a **zero** false-link rate in the
fixture. This is the measured basis for the PRD's instruction that prospective
instrumentation begins in month one: the run marker is what drives false links
toward zero, not any post-hoc scoring.

## 3. Measured linkage behaviour (AC1)

Reproduce with `python3 -m pytest tests/outcomes/test_identity.py`. The fixture
(`tests/outcomes/fixtures.py`) embeds three traps: a digest collision across two
real changes, a digest collision with an unrelated change, and a trial with no
production change. Measured on 8 trials:

| Floor | Predicted | True | False link | Missed | Precision | Recall | False-link rate |
|-------|-----------|------|-----------|--------|-----------|--------|-----------------|
| `LOW` (default) | 6 | 5 | 1 | 1 | 0.833 | 0.833 | 0.167 |
| `HIGH` | 4 | 3 | 1 | 3 | 0.750 | 0.500 | — |

Per-confidence-tier: `CONFIRMED` false-link rate 0.0 (precision 1.0); the single
false link lands in the `HIGH` tier (the digest collision). Raising the floor
trades recall for nothing on the false-link count. These numbers are asserted
exactly in the test suite so an independent reviewer reproduces them (AC4).

Two further failure modes are exercised as targeted tests kept *out* of the
eight-trial metric fixture (so the numbers above stay stable): a cross-signal
same-tier collision (`patch_digest` → decoy, `commit_sha` → real change, both
HIGH) is refused as ambiguous, and a same-PR-number candidate in a *different*
repository is not linked (repo scoping). See
`test_cross_signal_same_tier_collision_is_ambiguous` and
`test_cross_repo_candidate_is_not_linked`.

## 4. Outcome taxonomy (AC2)

Six outcomes are observed separately, each with one operational definition, an
observation window, censoring rules, competing risks, an identity-confidence
floor, and the allowed claim language. Encoded in
`src/codeprobe/outcomes/outcomes.py`.

| Outcome | Window | Days | Censoring | Competing risks | Id floor | Claim |
|---------|--------|------|-----------|-----------------|----------|-------|
| `code_survival` | hybrid | 30/60/90 | right, administrative | revert, takeover | HIGH | association |
| `revert` | event | 30/60/90 | right | — | HIGH | descriptive |
| `corrective_churn` | hybrid | 30/60/90 | right, competing | revert | HIGH | association |
| `review_burden` | event (merge) | — | right | takeover | MEDIUM | descriptive |
| `incident` | fixed | 90 | right, administrative | revert | HIGH | association |
| `human_takeover` | event | — | right | revert | MEDIUM | descriptive |

**Window selection.** Survival and corrective churn are hybrid: event-primary
(first overwrite / first corrective touch) with fixed 30/60/90-day reporting
checkpoints, because a pure fixed window discards the timing signal and a pure
event window cannot be aggregated across changes. Revert is event with the same
checkpoints. Review burden and takeover are bounded by the merge/close event.
Incident is fixed at 90 days alone because incidents lag and a shorter window
systematically under-counts.

**Counterexamples** are recorded per outcome in the code (the deliberately
excluded case), e.g. a pure-formatting reflow reads as 0% survival though nothing
was semantically undone; an incident touching the same service but tracing to a
different change is not attributed on proximity.

## 5. Cohort validity rules (AC3)

**Selected strategy: stratified-exact matching on structural keys with a
covariate-balance check on residual confounders.** Encoded in
`src/codeprobe/outcomes/cohorts.py`.

- **Strata** are exact-match keys over mechanical facts: `repo`,
  `change_topology` (single_file / multi_file / cross_module), `ownership_surface`
  (single_owner / multi_owner / unowned from CODEOWNERS), `risk_tier` (structural
  flags: touches tests / migration / config).
- **Exclusions.** A stratum is admitted only when both arms (agent and
  human/baseline) are present; unmatched units are excluded and recorded, never
  silently compared. Units below the identity-confidence floor are excluded
  before matching.
- **Confounders.** Residual covariates (e.g. change size) are checked with the
  standardised mean difference between arms. `covariate_balance()` returns the
  raw SMD per covariate, restricted to the covariates the spec names as
  `confounders` (so the check reflects recorded intent, not whatever covariates
  happen to be attached); it measures and never gates. The conventional 0.1
  reference is documentation, not an enforced threshold. A zero within-arm
  variance with differing means returns `inf` (SMD is genuinely undefined there),
  never a falsely-balanced `0.0`.
- **Identity floor is enforced, not just declared.** `CohortSpec.for_outcome(kind)`
  builds a spec whose `min_link_confidence` equals the outcome's declared floor,
  and `admits_outcome(spec, kind)` returns whether a cohort's floor is strict
  enough to back that outcome's claim. Report assembly (epic `codeprobe-tsi9.5`)
  calls `admits_outcome` before attributing a cohort to an outcome, so a
  `CODE_SURVIVAL` claim cannot be assembled from a cohort that admitted
  `MEDIUM`-identity members. The discipline the design advertises is wired into
  code, not left to prose.
- **Censoring / competing risks.** Carried per outcome (§4). Survival admits
  right and administrative censoring; corrective churn treats revert as a
  competing event.

Measured (`tests/outcomes/test_cohorts.py`): membership is deterministic and
reproducible across repeated builds; an engineered agent-vs-human size imbalance
surfaces as SMD 10.0, far above the 0.1 reference, demonstrating the balance
check catches confounding the exact strata do not absorb.

## 6. Causal discipline and claim language

The bridge is observational. The claim-language enum offers only `descriptive`
and `association`; no outcome may be labelled causal. Rates are descriptive;
matched-cohort comparisons are associations reported with their censoring and
residual imbalance. A claim is admissible only above the outcome's identity
floor; below it, the outcome is reported as `human-required` / insufficient
identity rather than asserted. Selection bias (which changes reach production),
right censoring (outcomes not yet observed), and competing risks (revert ending
a survival observation) are declared with every outcome, not footnoted.

### ZFC compliance

This module sits in the deterministic-statistics + policy-enforcement lane.
Identity precedence is a fixed policy ordering; matching is exact equality;
strata are structural VCS/file facts; the balance statistic is standardised
arithmetic; claim-language restriction is policy enforcement. No semantic
classification, heuristic scoring, or tunable meaning-threshold lives in the
code. Where a semantic risk tier is ever wanted, it is produced upstream by a
model and passed in as an opaque string.

### Privacy and data minimisation

The linkage contract will eventually ingest partner production records, so the
minimisation rules are fixed here, before the first connector is written, and
bind to the retention/redaction/deletion policy under `codeprobe-tsi9.11` (the
sibling decision in the same PRD table). `ChangeRecord.embedded_markers` holds
only the matched marker *tokens*, never the raw commit message or PR body: those
free-text fields routinely carry author names, emails, and internal ticket
references, and storing them would over-collect PII the linkage does not need. A
connector extracts the marker token and discards the surrounding text. Raw
partner identifiers used only for linkage inherit `tsi9.11`'s retention window;
this module holds no other partner free-text. The docstrings on
`ChangeRecord`/`TrialFingerprint` state these preconditions so the epic
`codeprobe-tsi9.5` connectors cannot over-collect by omission.

## 7. Alternatives considered and calibration-pending items

- **Patch digest as primary identity** — rejected as primary (kept as `HIGH`
  secondary) because digest collisions produce confident false links a
  confidence floor cannot remove (§2, measured §3).
- **Fixed 30/60/90 windows for every outcome** — rejected for survival, churn,
  revert, review, takeover because it discards timing and merge-boundary
  structure; kept only for incidents where lag dominates.
- **Propensity-only matching** — rejected as primary because it hides the
  structural strata that carry most confounding and is harder to reproduce;
  retained as an adjustment layer on residual covariates.
- **Pooling all outcomes into one survival metric** — rejected; competing risks
  (a reverted change cannot also be a surviving change) require separate
  definitions with declared competing events.

**Pending partner-corpus calibration.** Three evidence items in the bead require
partner data that is acquired under epic `codeprobe-tsi9.1`: the partner
identifier availability audit (which of run_marker / digest / commit / PR are
actually recoverable per partner), the false-link/missed-link study on real
history, and the reproducible cohort prototype on a partner repo. The
methodology, types, and measurement code are landed here and validated on the
synthetic fixture; the numbers must be re-measured on each partner corpus before
survival claims ship. This document is the reproducible harness for that
re-measurement, not a claim that partner linkage error is already known.

## 8. Acceptance-criteria mapping

1. *Linkage error behaviour is measured* — §3, `test_identity.py` (exact
   precision/recall/false-link/missed-link on a trap-bearing fixture).
2. *Every outcome has an operational definition and observation window* — §4,
   `test_outcomes_registry.py` (completeness + window invariants).
3. *Cohort rules state variables, exclusions, censoring, confounders* — §5,
   `test_cohorts.py` (deterministic membership + balance).
4. *Independent review reproduces membership* — the entire suite asserts exact
   values; `python3 -m pytest tests/outcomes/` reproduces them.
