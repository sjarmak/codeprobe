# Oracle Checks Demo Task

Demonstration task for the `oracle_checks` scorer_family. The task asks
the agent to implement a small function under three rubric criteria:

1. **handles_edge_case_x** — empty-input handling
2. **covers_error_branches** — exhaustive error-path coverage
3. **preserves_public_api** — no breaking changes to public symbols

Each criterion is scored by a dedicated verifier script in
`tests/verifiers/`. The composite reward is the weight-normalized
average of the three criterion scores.
