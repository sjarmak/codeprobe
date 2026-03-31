# PRD: codeprobe

## Problem Statement

AI coding agent benchmarks use fixed task sets (SWE-bench, HumanEval) that models may have memorized from training data, producing inflated scores. Teams evaluating whether to adopt Claude, Copilot, Cursor, or other agents have no way to test them against their own codebase — the code that actually matters.

codeprobe solves this by mining eval tasks from your repo's own history (merged PRs/MRs), producing benchmarks that are impossible to contaminate. It runs any agent against these tasks, scores the results, and tells you which setup works best for YOUR code.

## Goals

- pip-installable CLI with 5 commands: init, mine, run, interpret, assess
- Zero-to-first-result in under 5 minutes
- Agent-agnostic via AgentAdapter Protocol (ships with Claude + Copilot)
- Support mining from GitHub, GitLab, Bitbucket, Azure DevOps, Gitea, local repos
- .evalrc.yaml as the single user-facing config format
- Alpha release on PyPI to validate with 3-5 external testers

## Non-Goals

- SaaS dashboard or hosted service (v1+ consideration)
- Plugin marketplace or task pack registry (v1+ consideration)
- More than 2 built-in agent adapters (community can add via entry_points)
- Exposing experimental analysis methods (SPRT, tournament, Elo) as CLI commands (kept in contrib/)
- IDE integration

## Requirements

### Must-Have (v0.1.0-alpha)

- `codeprobe init` interactive wizard (port /experiment Phase 0)
- `codeprobe mine` task extraction from repo history
- `codeprobe run` agent execution with scoring
- `codeprobe interpret` results analysis and recommendations
- `codeprobe assess` codebase benchmarking potential
- AgentAdapter Protocol with Claude and Copilot adapters
- Configurable permission mode (no hardcoded bypassPermissions)
- Path discovery via shutil.which (no hardcoded paths)
- .evalrc.yaml as primary config, experiment.json as internal state
- 14+ passing tests, CI-ready

### Should-Have (v0.2.0-beta)

- Progress bars and colored output (rich or click.style)
- "What's next" footer on every command
- SPRT analysis graduated from contrib/ to `codeprobe analyze sprt`
- CI gate command (`codeprobe ci gate`)
- Test.sh sandboxing (temp directory copy, timeout, resource limits)
- GitHub Action wrapper

### Nice-to-Have (v1.0.0)

- entry_points discovery for third-party agent adapters
- Task packs (shareable benchmark suites)
- HTML report generation (port browse_results.py)
- Stable public API for library usage

## Design Considerations

### Layered Architecture

```
CLI (click)  →  Core (mine/run/interpret/assess)  →  Models (frozen dataclasses)
                     ↓                                        ↑
              Adapters (AgentAdapter Protocol)          Config (.evalrc.yaml)
                     ↓
              Contrib (SPRT, tournament, Elo — library only, not CLI)
```

### Key Trade-offs

1. **Minimal CLI vs rich analysis**: Ship 5 commands now, graduate contrib/ features based on demand
2. **Hardcoded agents vs plugin system**: Ship Protocol + 2 adapters, add entry_points discovery when third agent is needed
3. **Security vs convenience**: Default to safe permission mode, require explicit opt-in for bypass

## Open Questions

- What is the right sandboxing model for test.sh execution? Docker? temp dir + timeout? seccomp?
- Should .evalrc.yaml support inline task definitions or only reference task directories?
- How should the tool handle non-determinism across agent runs? Average of N runs? Confidence intervals?

## Research Provenance

Synthesized from 5 divergent research agents (prior art, technical architecture, UX/adoption, failure modes, ecosystem/extensibility), a 30-idea brainstorm session, and a 3-position structured debate (focused product vs extensible framework vs research-first skeptic). The "layered launch" approach emerged as a synthesis position during Round 2 of the debate.
