# Strategy Review Evidence: Repository Intelligence and Proof Framework

- **Bead:** `codeprobe-tsi9`
- **Review date:** 2026-07-10
- **Canonical decision artifact:** [Repository Intelligence and Proof Framework PRD](../../strategy/repository_intelligence_proof_framework.md)

## The review started from repository evidence

The review traced CodeProbe's current pipeline from task mining through execution, scoring, interpretation, snapshots, and investigation writeups. It inspected the domain models, oracle curation, trace database, scoring contract, statistical analysis, report renderer, calibration gate, discovery artifacts, advanced `contrib` prototypes, and the existing Sourcegraph comparison campaigns.

The strongest existing foundations were:

- scorer-family, sub-score, diagnostic, verdict, and materialization lineage in `src/codeprobe/core/scoring/result.py`;
- ordered tool events in `src/codeprobe/trace/store.py`;
- per-file backend and curator provenance in `src/codeprobe/mining/oracle_curator.py`;
- deterministic statistical comparisons in `src/codeprobe/analysis/stats.py`;
- redacted and attested snapshots in `src/codeprobe/snapshot/`;
- consensus, fairness, bias, calibration, refresh, and trace-quality gates distributed across the repository.

The central gap was connective: the report model cannot link a decision claim to those artifacts, and the runtime model stops before review and production outcomes.

## Existing investigations changed the strategy

Three CodeProbe campaigns were treated as product evidence rather than isolated result corrections:

1. `docs/investigations/codeprobe-mcn7/eval_writeup.md` showed that a Sourcegraph SDLC delta of `+0.054` collapsed to `+0.0035` under repeated trials and was not significant at the task-family level.
2. `docs/investigations/codeprobe-evjr-r4/eval_writeup.md` showed a full Sourcegraph arm looping on `read_file`, while a narrowed arm made zero MCP calls and returned to the local edit-and-test workflow.
3. `docs/investigations/codeprobe-2txc/eval_writeup.md` traced an oracle-check failure to a Sourcegraph false-negative cascade that preamble wording could not repair.

These results ruled out preamble tuning, tool-presence A/B tests, and aggregate reward deltas as the main six-month product story. They created the case for treatment fidelity, repository-semantic interventions, task integrity, and evidence reachability.

## Competitive research established exclusion zones

The review used primary documentation and papers available on July 10, 2026:

- [OpenAI's SWE-Bench Pro audit](https://openai.com/index/separating-signal-from-noise-coding-evaluations/)
- [SWE-bench](https://github.com/SWE-bench/SWE-bench)
- [SWE-rebench](https://arxiv.org/abs/2505.20411)
- [SWE-bench-Live](https://arxiv.org/abs/2505.23419)
- [Inspect AI evaluation logs](https://inspect.aisi.org.uk/eval-logs.html)
- [LangSmith evaluation concepts](https://docs.langchain.com/langsmith/evaluation-concepts)
- [Braintrust experiment comparison](https://www.braintrust.dev/foundations/comparing-experiments)
- [TraceProbe](https://arxiv.org/abs/2607.06184)
- [Causal Agent Replay](https://arxiv.org/abs/2606.08275)
- [AgenTracer](https://openreview.net/forum?id=l05DseqvuD)
- [REFLECT](https://doi.org/10.48550/arXiv.2606.09071)
- [Anthropic's Claude Code session analysis](https://www.anthropic.com/research/claude-code-expertise)
- [Sourcegraph Deep Search](https://sourcegraph.com/docs/deep-search)
- [Sourcegraph Agentic Batch Changes](https://sourcegraph.com/docs/agentic-batch-changes)
- [in-toto Attestation Framework](https://github.com/in-toto/attestation)
- [OpenTelemetry GenAI semantic conventions](https://github.com/open-telemetry/semantic-conventions-genai)

The resulting exclusion zones were clear: public leaderboards, generic live task mining, trace viewers, canonical action taxonomies, generic counterfactual step replay, authenticated provenance envelopes, and executive adoption dashboards already have credible implementations. CodeProbe should integrate with those standards and products rather than reproduce their shape.

## Divergence tested thirty architectural shapes

A constrained brainstorming pass cataloged eleven prior-art families, generated thirty shape-distinct ideas, and used small prototypes to reject implementations whose computational structure duplicated an earlier idea. Three candidates were excluded during convergence:

- cross-agent trajectory IR became plumbing after TraceProbe established the category;
- abductive failure replay overlapped Causal Agent Replay, AgenTracer, and REFLECT;
- a repository-constitution concept collapsed into the benchmark-integrity and alternate-solution mechanisms.

The highest-rated shapes were the claim-to-evidence graph, evaluation-integrity sentinel, requirement-to-verification matrix, temporal repository twin, repository-semantic field trial, production outcome bridge, and evidence-grounded compiler. Overlap analysis combined these into the six bets recorded in the PRD.

## Independent review narrowed the defensibility claims

Two read-only verification passes checked exact repository paths, current prior art, program dependencies, and every requested value dimension. Both returned pass with framing caveats:

- in-toto already owns authenticated provenance, so CodeProbe's novelty is temporal evaluation-claim lineage and the accumulated private graph;
- TraceProbe already owns generic action normalization and divergence diagnostics, so the context lab must measure semantic repository facts under controlled interventions;
- Sourcegraph already owns cited cross-repository understanding and execution, so the capability atlas must provide empirically calibrated agent fitness rather than another architecture map;
- the executive compiler is a product surface whose defensibility comes from the underlying proof, integrity, context, and outcome evidence;
- production survival cohorts require identity confidence, censoring, matching, confounder reporting, and disciplined separation of association, prediction, and causation;
- partner acquisition and outcome instrumentation start immediately because neither can be reconstructed at the end of the roadmap.

## Tracker state

The roadmap is tracked under root epic `codeprobe-tsi9`. Its eight delivery epics are `codeprobe-tsi9.1` through `codeprobe-tsi9.8`; architectural decisions are `codeprobe-tsi9.9` through `codeprobe-tsi9.14`. Decision-to-implementation gate tasks live under the proof, outcome, fitness, and compiler epics.

Live status and dependencies belong to Beads. This investigation records how the strategy was derived; the PRD records the resulting architecture.
