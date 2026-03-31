"""Contrib — experimental analysis methods (SPRT, tournament, Elo, etc.).

These modules are importable as library code but not exposed as CLI subcommands.
They will be graduated to CLI commands based on user demand.

Available modules:
    - sprt: Sequential Probability Ratio Test for early stopping
    - tournament: Round-robin tournament ranking
    - elo: Elo rating system for config comparison
    - counterfactual: Find tasks where configs diverge
    - mutation: Sensitivity analysis via score perturbation
    - fingerprint: Agent pass/fail signature vectors
    - debate: Structured argument-based comparison
    - decision_tree: Feature-based performance splitting
    - pareto: Pareto front for cost-quality trade-offs
    - adaptive: Adaptive task sampling for efficient evaluation
"""
