# PRD: Repository Intelligence and Proof Framework

- **Status:** proposed
- **Date:** 2026-07-10
- **Roadmap horizon:** July through December 2026
- **Tracking system:** Beads root epic `codeprobe-tsi9` and child epics linked through `spec_id`
- **Applies to:** mining, execution, tracing, scoring, analysis, snapshots, customer evidence, and future Sourcegraph product integration

## Two audits exposed the real product

OpenAI reported on July 8, 2026 that roughly 30 percent of SWE-Bench Pro's 731 public tasks were broken. Its agent pipeline flagged 27.4 percent, a five-engineer annotation campaign identified 34.1 percent, and the failures included overly strict tests, underspecified or misleading prompts, and low test coverage. OpenAI retracted its earlier recommendation to adopt the benchmark.

CodeProbe had already found the same class of problem from another direction. A Sourcegraph SDLC advantage initially reported as `+0.054` collapsed to `+0.0035` over repeated trials, with a 95 percent confidence interval that included zero. A separate investigation showed that a full Sourcegraph arm spent its turn budget in `read_file` loops, while a narrowed arm made zero MCP calls and returned to the local edit-and-test workflow. Another run traced a failure to a Sourcegraph false-negative cascade rather than prompt quality.

These are not isolated benchmark corrections. They identify CodeProbe's product: a repository-grounded system of record for claims about AI engineering performance. The system must establish that an evaluation is valid, identify which repository-specific mechanism caused an outcome, quantify where an agent is fit to operate, follow its work after the session, and compile the evidence into a decision artifact that can survive technical and executive review.

Primary evidence:

- [OpenAI, "Separating signal from noise in coding evaluations"](https://openai.com/index/separating-signal-from-noise-coding-evaluations/)
- [CodeProbe repeated-trial Sourcegraph result](../investigations/codeprobe-mcn7/eval_writeup.md)
- [CodeProbe Sourcegraph tool-surface investigation](../investigations/codeprobe-evjr-r4/eval_writeup.md)
- [CodeProbe false-negative cascade investigation](../investigations/codeprobe-2txc/eval_writeup.md)

## The repository contains the parts but not the proof chain

CodeProbe already records more useful evidence than its current report model can express. `ScoreResult` declares a scorer family, sub-scores, diagnostics, verdict, and materialization method. The trace store records ordered tool inputs, outputs, latency, and token counts. Oracle curation retains backend provenance, tier, model-review rationale, and quarantined disagreements. Snapshots hash files, apply redaction and canary checks, record dependency surfaces, and optionally attach an HMAC attestation. Statistical analysis includes paired tests, confidence intervals, and effect sizes.

Those records remain separate artifacts. The report object contains summaries, rankings, comparisons, and per-task results, while the HTML executive summary prints the winning configuration, pass rate, mean score, and cost. It cannot express a versioned claim, cite the exact evidence supporting that claim, propagate oracle uncertainty, distinguish correlation from an intervention result, or connect an offline score to review and production outcomes.

Repository foundations:

- [`ScoreResult`](../../src/codeprobe/core/scoring/result.py)
- [trace database schema](../../src/codeprobe/trace/store.py)
- [oracle curator](../../src/codeprobe/mining/oracle_curator.py)
- [snapshot manifest and dependency surface](../../src/codeprobe/snapshot/manifest.py)
- [snapshot redaction and attestation](../../src/codeprobe/snapshot/redact.py)
- [statistical analysis](../../src/codeprobe/analysis/stats.py)
- [current report model](../../src/codeprobe/analysis/report.py)

The customer-data foundation is also incomplete. The enterprise discovery gate still shows all three partner artifacts pending, and the calibration contract requires at least 100 tasks across at least three private repositories with two independent curators. The code path exists; the partner corpus does not.

- [enterprise discovery status](../discovery/README.md)
- [calibration contract and partner-data requirements](../CALIBRATION.md)

## The market has made several features commodity

The roadmap must avoid categories that already have credible implementations. Competing on those shapes would produce a useful tool without producing a durable advantage.

| Existing category | Representative prior art | CodeProbe boundary |
| --- | --- | --- |
| Static issue-resolution benchmark | [SWE-bench](https://github.com/SWE-bench/SWE-bench) | Ingest its format; do not build another public leaderboard. |
| Continuously mined public tasks | [SWE-rebench](https://arxiv.org/abs/2505.20411), [SWE-bench-Live](https://arxiv.org/abs/2505.23419) | Focus on private workload representativeness, integrity, lineage, and retirement. |
| Trace storage and experiment comparison | [Inspect AI logs](https://inspect.aisi.org.uk/eval-logs.html), [LangSmith evaluation](https://docs.langchain.com/langsmith/evaluation-concepts), [Braintrust experiments](https://www.braintrust.dev/foundations/comparing-experiments) | Adopt compatible ingestion; do not make a trace viewer the flagship. |
| Canonical action taxonomies and trajectory anti-patterns | [TraceProbe](https://arxiv.org/abs/2607.06184) | Treat normalization as plumbing. Measure repository-semantic context contribution instead. |
| Causal step replay | [Causal Agent Replay](https://arxiv.org/abs/2606.08275), [AgenTracer](https://openreview.net/forum?id=l05DseqvuD), [REFLECT](https://doi.org/10.48550/arXiv.2606.09071) | Intervene on code-graph facts, search affordances, history, ownership, and verification, then connect results to production. |
| Code graph and cross-repository execution | [Sourcegraph Deep Search](https://sourcegraph.com/docs/deep-search), [Agentic Batch Changes](https://sourcegraph.com/docs/agentic-batch-changes) | Add empirical fitness, integrity, outcome evidence, and proof to these product surfaces. |
| Authenticated software provenance | [in-toto Attestation Framework](https://github.com/in-toto/attestation) | Emit compatible predicates; do not invent another envelope or signing standard. |
| Executive adoption and delivery dashboards | GitHub, DX, LinearB, Sourcegraph Analytics | Base recommendations on validated tasks, causal contribution, uncertainty, and post-merge outcomes rather than usage proxies. |

Generic trace classification, generic causal replay, another export format, broad adapter expansion, and more preamble tuning are explicit non-goals for this horizon.

## The proof ledger gives every claim an address

### Bet 1: Temporal Repository Evidence Graph and Proof Ledger

Build a bitemporal evidence graph connecting repository snapshots, requirements, tasks, oracle observations, experimental treatments, trace spans, patches, verifier results, production outcomes, and report claims. Every node has a stable identity and content digest; every material claim has a replayable support path to immutable leaves.

The graph does not replace the existing trace database, scoring records, or snapshot bundle. It assigns durable identities to them and records their semantic relationships. Attestations use in-toto-compatible predicates, while OpenTelemetry GenAI conventions provide an ingestion boundary for external traces.

**Why it is hard to copy:** The schema is medium-defensibility infrastructure. The high-defensibility asset is the accumulated private graph spanning repository history, cross-repository semantics, agent behavior, verifier decisions, reviews, incidents, and customer outcomes.

**Implementation complexity:** Very high. The critical problems are identity resolution, schema evolution, temporal queries, tenant isolation, retention, deletion, source redaction, evidence reachability, and claim invalidation when an upstream node changes.

**Expected value:** customer 5/5, research 5/5, demo 5/5.

**Customer evidence:** A buyer can open a recommendation and inspect the task source, oracle construction, configuration, trace, patch, verifier, statistical calculation, and outcome evidence without reconstructing the run.

**Sourcegraph product path:** Shared evidence substrate for Deep Search, Agentic Batch Changes, agent analytics, evaluation, and future autonomy policy.

## Task integrity comes before agent ranking

### Bet 2: Living Private Benchmark Foundry and Integrity Sentinel

Turn mining into a governed benchmark supply chain. Every task passes requirement-to-test concordance, prompt ambiguity review, alternate-valid-solution testing, verifier mutation testing, environment crossover checks, oracle disagreement analysis, contamination checks, and drift rules. Tasks carry one of four states: `admissible`, `contested`, `quarantined`, or `expired`.

The foundry composes existing fairness scans, calibration triads, multi-backend oracle curation, cross-validation, bias detection, task refresh, and snapshots. It does not rebuild them. The new work is a single task-integrity contract, an audit history, retirement semantics, uncertainty propagation, and active selection of the next evidence-producing task.

**Why it is hard to copy:** Private task supply, alternate-solution adjudication, verifier failures, repository drift, and customer-specific workload coverage accumulate into a corpus that cannot be scraped from public leaderboards.

**Implementation complexity:** Very high. Alternate-solution validation, human adjudication, infrastructure reproducibility, and calibrated confidence are the critical paths.

**Expected value:** customer 5/5, research 5/5, demo 4/5.

**Customer evidence:** A ranking can state which tasks were admitted, which were rejected, why their tests were trusted, and how oracle uncertainty affects the conclusion.

**Sourcegraph product path:** Continuous validation service for Sourcegraph agents and third-party agents operating on a customer's repositories.

## Repository facts become experimental treatments

### Bet 3: Semantic Context Contribution Lab

Represent experiments with a typed treatment schema: hypothesis, control, independent variable, repository-semantic mediator, expected mechanism, falsification condition, treatment-fidelity observation, and validity requirements. Treatments operate on retrieved symbols, dependency edges, search result ordering, ownership and history context, local versus Sourcegraph lookup, verification visibility, and cross-repository scope.

Run paired or randomized ablations and measure time to the first relevant symbol, localization correctness, edit completeness, verification behavior, cost, and final outcome. Semantic explanations are delegated to models under ZFC, but every explanation cites trace spans and repository facts and remains a hypothesis until a distinguishing intervention survives replay.

Repeated trials are clustered by task. Trial rows are not treated as independent samples when the intervention is assigned at the task level.

**Why it is hard to copy:** The treatment-response corpus is grounded in private code-graph facts and observed across agents. Generic agent traces lack those semantic identities and cannot establish which repository fact changed the result.

**Implementation complexity:** Very high. It requires treatment orchestration, Sourcegraph graph integration, repeated trials, fidelity checks, causal design, and model-generated explanations with mechanical citation validation.

**Expected value:** customer 5/5, research 5/5, demo 5/5.

**Customer evidence:** The report can distinguish "Sourcegraph was present" from "this dependency edge, returned by this search, changed localization and produced a passing patch."

**Sourcegraph product path:** Context-quality measurement, retrieval-policy tuning, and regression monitoring for Deep Search and MCP surfaces.

## Session success is not production success

### Bet 4: Production Outcome Bridge and Survival Cohorts

Join evaluation and agent-run identities to PR acceptance, time to merge, review rounds, review severity, CI recovery, corrective churn, follow-up fixes, reverts, escaped defects, incidents, human takeover, and code survival. Every outcome metric declares its observation window, censoring rules, competing outcomes, and identity confidence.

The first product is retrospective: link historical agent changes and matched human changes by repository, change topology, ownership surface, and risk. Prospective instrumentation begins in the first month because six-month outcome cohorts cannot be recreated reliably from a final report.

Anthropic's June 2026 analysis covers roughly 400,000 Claude Code sessions but explicitly states that it cannot observe whether generated code is later used, discarded, or economically valuable. Cross-agent, post-merge evidence is the available white space.

- [Anthropic, "Agentic coding and persistent returns to expertise"](https://www.anthropic.com/research/claude-code-expertise)

**Why it is hard to copy:** The moat is the longitudinal private join between agent sessions, repository changes, reviews, incidents, and later corrective work. Agent vendors usually lose visibility after the session ends.

**Implementation complexity:** Very high. Connectors are straightforward relative to identity linkage, privacy, selection bias, right censoring, causal overclaim prevention, and stable outcome definitions.

**Expected value:** customer 5/5, research 5/5, demo 4/5.

**Customer evidence:** Replace "agent score 0.71" with calibrated evidence about review burden, revert risk, corrective churn, and durable production value.

**Sourcegraph product path:** Agent quality and code-health analytics spanning investigation, change, review, and production.

## Quality becomes a calibrated fitness surface

### Bet 5: Repository Capability Atlas and Agent Fitness Routing

Replace global rankings with a calibrated model of where each agent and policy is fit to operate. The atlas combines a partner-derived capability ontology, repository topology, task archetypes, scorer and checkpoint subscores, context-contribution results, production outcomes, and multidimensional item-response or Bayesian models.

The output is a distribution over quality, cost, latency, review burden, and post-merge risk for each agent-policy combination. Active evidence campaigns choose the next task or treatment by expected information gain. Routing includes an explicit `human-required` result when confidence or risk constraints are not satisfied.

Existing `contrib` modules provide seams but not the capability: fingerprints are ordered score vectors, counterfactual analysis only finds divergent pass/fail outcomes, and adaptive sampling shuffles randomly.

- [current fingerprint prototype](../../src/codeprobe/contrib/fingerprint.py)
- [current counterfactual prototype](../../src/codeprobe/contrib/counterfactual.py)
- [current adaptive prototype](../../src/codeprobe/contrib/adaptive.py)

**Why it is hard to copy:** A static heatmap is easy to reproduce. A fitness model calibrated on customer task topology, controlled context treatments, and post-merge outcomes depends on the preceding four systems and grows stronger with use.

**Implementation complexity:** High to very high. Ontology governance, item discrimination, minimum-data rules, held-out routing validation, uncertainty calibration, repository drift, and agent drift all matter.

**Expected value:** customer 5/5, research 5/5, demo 5/5.

**Customer evidence:** For a proposed change, show predicted success, cost, risk, confidence, the evidence campaign behind the estimate, and the recommended agent or review policy.

**Sourcegraph product path:** Agent selection, rollout boundaries, and autonomy policy for Deep Search and Agentic Batch Changes.

## Reports become compiled claims

### Bet 6: Executive Evidence Compiler and Proof-Carrying Change Passports

Compile role-specific artifacts from the proof ledger. A VP Engineering view states the decision, confidence, rollout boundary, and durable outcome evidence. Platform teams receive capability gaps, context contribution, and tool ROI. Finance receives quality-adjusted cost and sensitivity. Security and procurement receive dataflow, integrity, fairness, and reproducibility. Research receives methodology, uncertainty, counterevidence, and raw evidence references.

Every material sentence links to a claim node. The compiler refuses to recommend when task integrity, calibration, treatment fidelity, or evidence reachability fails. Change passports extend in-toto predicates with CodeProbe evidence identifiers rather than creating a new envelope.

The report renderer is not the moat. Its value comes from the proof, integrity, intervention, capability, and production evidence it compiles.

**Why it is hard to copy:** Rendering has low defensibility. A role-specific document whose claims are validated against a private temporal evidence graph has high defensibility because competitors do not possess the underlying evidence.

**Implementation complexity:** Medium to high. The work includes a claim schema, audience projections, counterevidence, refusal rules, stable citations, attestation predicates, and the partner-selected delivery surface.

**Expected value:** customer 5/5, research 2/5, demo 5/5.

**Customer evidence:** One command produces an executive brief, technical appendix, security packet, and signed change passport with one-click evidence drill-down.

**Sourcegraph product path:** Decision and audit surface attached to agentic investigations and batch changes.

## Six months produce one end-to-end proof

| Month | Program outcome | Exit artifact |
| --- | --- | --- |
| 1 | Complete the three partner discovery artifacts; define evidence identities, temporal relationships, tenant/privacy policy, and OTel/in-toto boundaries. Begin outcome ingestion. | Evidence schema v1, partner agreements, initial outcome event store. |
| 2 | Ship the Integrity Sentinel and requirement-to-verification matrix. Run the first private calibration campaign across at least 100 tasks and three repositories. | Admitted private task bank with integrity histories and calibration profile. |
| 3 | Ship typed treatment manifests and the first Semantic Context Contribution experiments. Reproduce the existing false-negative and tool-abandonment cases through controlled interventions. | Falsifiable, evidence-cited failure attribution report. |
| 4 | Land the Production Outcome Bridge and retrospective matched cohorts. | Offline-to-production calibration study with declared censoring and confounders. |
| 5 | Build the Capability Atlas, active evidence campaigns, and uncertainty-aware routing prototype. | Held-out fitness predictions and policy recommendations. |
| 6 | Ship the Executive Evidence Compiler and change passports. Demonstrate the full chain on a Sourcegraph workflow. | Trustworthy task, causal context result, fitness recommendation, production evidence, and signed executive proof. |

Outcome instrumentation begins in month one even though its analysis lands later. The proof ledger and integrity contract precede the context lab. The context lab and outcome bridge precede the capability atlas. The executive compiler consumes all prior artifacts and lands last.

## Weak evidence cannot become a strong claim

The framework enforces the following refusal rules:

1. A report claim without a complete support path is invalid.
2. A quarantined, contested, or expired task cannot contribute to a headline comparison unless the report explicitly scopes the claim to an integrity investigation.
3. Oracle uncertainty propagates into score and claim confidence; downstream aggregation cannot restore confidence lost upstream.
4. A semantic failure explanation without a distinguishing intervention is labeled `hypothesis`, never `finding`.
5. A treatment arm that did not exercise its assigned surface fails treatment fidelity and cannot establish a tool effect.
6. Routing predictions require held-out calibration and an uncertainty interval. Insufficient evidence produces `human-required`.
7. Production outcomes declare observation windows, censoring, selection criteria, and known confounders.
8. Executive recommendations require partner calibration, evidence reachability, and a passing integrity state.

These gates are structural policy. Semantic judgments remain model-delegated under ZFC, while application code validates schemas, evidence references, lifecycle states, and deterministic calculations.

## Scope cuts preserve the compounding asset

If capacity contracts, cut delivery polish, federated cross-customer learning, sealed challenge protocols, multi-agent portfolio composition, and secondary export targets first. Do not cut partner data acquisition, benchmark integrity, outcome instrumentation, or the proof graph. Those four assets compound; every other capability consumes them.

The following work remains outside this horizon:

- a public global agent leaderboard;
- a proprietary trace standard;
- a generic trajectory anti-pattern taxonomy;
- generic causal replay detached from repository semantics;
- a replacement for in-toto or OpenTelemetry;
- new adapters without a partner requirement;
- task-taxonomy expansion before discovery evidence;
- a dashboard whose claims cannot be traced to evidence;
- automatic production deployment or merge authority.

## Tracking separates strategy, work, and evidence

This PRD is the durable source for strategy, architecture, prior art, scope, and decisions. It does not carry live progress checkboxes.

The Beads root epic and child epics are the only live sources for status, ownership, priority, and dependencies. Every child references this document through `spec_id`, uses the label `roadmap:h2-2026`, and names its architectural bet through a `bet:*` label.

| Program area | Bead |
| --- | --- |
| Root roadmap | `codeprobe-tsi9` |
| Partner discovery and calibration | `codeprobe-tsi9.1` |
| Proof ledger | `codeprobe-tsi9.2` |
| Benchmark foundry and integrity | `codeprobe-tsi9.3` |
| Semantic context contribution | `codeprobe-tsi9.4` |
| Production outcomes | `codeprobe-tsi9.5` |
| Capability atlas and routing | `codeprobe-tsi9.6` |
| Executive evidence compiler | `codeprobe-tsi9.7` |
| End-to-end Sourcegraph demonstration | `codeprobe-tsi9.8` |

Evidence lives under `docs/investigations/<bead-id>/`. Research beads commit their writeups there. Implementation beads ship tests and code in the same commit. Closure requires a main-reachable artifact, reviewer verdict, reviewer identity, and a passing `scripts/check_bead_reachability.py` result.

The audit chain is:

```text
strategy claim
  -> PRD section
  -> bead and acceptance criteria
  -> investigation evidence
  -> reviewed commit reachable from main
  -> bead close metadata
```

## Decisions remain explicit

The following choices are unresolved and tracked as decision beads under the roadmap epic:

| Decision | Bead | Required evidence | Blocks |
| --- | --- | --- | --- |
| Evidence graph storage and schema | `codeprobe-tsi9.9` | scale model, migration plan, local/offline behavior, query examples | proof-ledger implementation |
| OTel and in-toto compatibility boundary | `codeprobe-tsi9.10` | loss analysis, predicate design, round-trip fixtures | external ingestion and passports |
| Private evidence retention and deletion | `codeprobe-tsi9.11` | partner security review, tenant lifecycle, redaction and export behavior | partner production data |
| Capability ontology and routing calibration | `codeprobe-tsi9.13` | partner taxonomy, minimum sample rules, held-out validation design | fitness routing |
| Executive recommendation refusal policy | `codeprobe-tsi9.14` | audience requirements, calibration gates, counterevidence behavior | evidence compiler |

Resolved decisions are recorded below with a date, bead identifier, selected option, rejected alternatives, and evidence link. Beads preserve the deliberation; this PRD preserves the resulting architecture.

| Decision | Bead | Date | Selected | Rejected | Evidence |
| --- | --- | --- | --- | --- | --- |
| Production outcome identity and cohort rules | `codeprobe-tsi9.12` | 2026-07-10 | Repo-scoped identity precedence `run_marker` > `patch_digest`/`commit_sha` > `pr_number` > `heuristic` with confidence tiers and joint same-tier ambiguity-refusal; six outcomes (survival, revert, corrective churn, review burden, incident, takeover) each with its window, censoring, competing risks, identity floor, and claim class; stratified-exact cohorts on structural keys with a standardised-mean-difference balance check; observational claim language only (`descriptive`/`association`, never causal). | Patch digest as primary identity (confident digest-collision false links a confidence floor cannot remove); uniform fixed 30/60/90 windows (discards timing and merge-boundary structure); propensity-only matching (hides structural strata, harder to reproduce); pooled single survival metric (ignores competing risks). | [design.md](../investigations/codeprobe-tsi9.12/design.md); `src/codeprobe/outcomes/`; `tests/outcomes/`. Partner-corpus linkage calibration pending under `codeprobe-tsi9.1`. |

The next implementation artifact is the proof-ledger schema, but the next program action is partner discovery. Building the schema without the customer evidence that will stress it would repeat the repository's own documented failure mode: the right mechanism for the wrong enterprise reality.
