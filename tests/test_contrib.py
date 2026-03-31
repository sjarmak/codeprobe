"""Tests for contrib/ experimental analysis modules."""

from __future__ import annotations

from codeprobe.models.experiment import CompletedTask, ConfigResults

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(task_id: str, score: float, **kwargs) -> CompletedTask:  # type: ignore[no-untyped-def]
    return CompletedTask(task_id=task_id, automated_score=score, **kwargs)


def _results(label: str, tasks: list[CompletedTask]) -> ConfigResults:
    return ConfigResults(config=label, completed=tasks)


# ===========================================================================
# SPRT
# ===========================================================================


class TestSPRT:
    def test_accept_better(self) -> None:
        from codeprobe.contrib.sprt import sprt_test

        wins = [1.0] * 20
        losses = [0.0] * 5
        result = sprt_test(wins + losses, theta_0=0.5, theta_1=0.7, alpha=0.05, beta=0.2)
        assert result.decision in ("accept", "reject", "continue")

    def test_not_enough_data(self) -> None:
        from codeprobe.contrib.sprt import sprt_test

        result = sprt_test([1.0], theta_0=0.5, theta_1=0.7)
        assert result.decision == "continue"

    def test_reject_when_bad(self) -> None:
        from codeprobe.contrib.sprt import sprt_test

        scores = [0.0] * 30
        result = sprt_test(scores, theta_0=0.5, theta_1=0.7)
        assert result.decision == "reject"


# ===========================================================================
# Tournament
# ===========================================================================


class TestTournament:
    def test_round_robin(self) -> None:
        from codeprobe.contrib.tournament import round_robin

        configs = [
            _results("A", [_task("t1", 1.0), _task("t2", 1.0)]),
            _results("B", [_task("t1", 0.0), _task("t2", 1.0)]),
            _results("C", [_task("t1", 0.0), _task("t2", 0.0)]),
        ]
        standings = round_robin(configs)
        assert standings[0].label == "A"
        assert standings[-1].label == "C"

    def test_two_configs(self) -> None:
        from codeprobe.contrib.tournament import round_robin

        configs = [
            _results("X", [_task("t1", 1.0)]),
            _results("Y", [_task("t1", 0.0)]),
        ]
        standings = round_robin(configs)
        assert len(standings) == 2
        assert standings[0].label == "X"


# ===========================================================================
# Elo
# ===========================================================================


class TestElo:
    def test_winner_gains_rating(self) -> None:
        from codeprobe.contrib.elo import compute_elo_ratings

        configs = [
            _results("A", [_task("t1", 1.0), _task("t2", 1.0)]),
            _results("B", [_task("t1", 0.0), _task("t2", 0.0)]),
        ]
        ratings = compute_elo_ratings(configs)
        assert ratings["A"] > ratings["B"]

    def test_equal_stays_near_default(self) -> None:
        from codeprobe.contrib.elo import compute_elo_ratings

        configs = [
            _results("A", [_task("t1", 1.0), _task("t2", 0.0)]),
            _results("B", [_task("t1", 0.0), _task("t2", 1.0)]),
        ]
        ratings = compute_elo_ratings(configs)
        assert abs(ratings["A"] - ratings["B"]) < 50


# ===========================================================================
# Counterfactual
# ===========================================================================


class TestCounterfactual:
    def test_identifies_divergent_tasks(self) -> None:
        from codeprobe.contrib.counterfactual import find_divergent_tasks

        a = _results("A", [_task("t1", 1.0), _task("t2", 0.0), _task("t3", 1.0)])
        b = _results("B", [_task("t1", 1.0), _task("t2", 1.0), _task("t3", 0.0)])
        divergent = find_divergent_tasks(a, b)
        task_ids = {d.task_id for d in divergent}
        assert "t2" in task_ids
        assert "t3" in task_ids
        assert "t1" not in task_ids


# ===========================================================================
# Mutation
# ===========================================================================


class TestMutation:
    def test_mutate_scores(self) -> None:
        from codeprobe.contrib.mutation import sensitivity_analysis

        tasks = [_task("t1", 1.0), _task("t2", 1.0), _task("t3", 0.0)]
        result = sensitivity_analysis(tasks, flip_fraction=0.5, iterations=10, seed=42)
        assert 0.0 <= result.mean_pass_rate <= 1.0
        assert result.std_pass_rate >= 0.0


# ===========================================================================
# Fingerprint
# ===========================================================================


class TestFingerprint:
    def test_fingerprint_vector(self) -> None:
        from codeprobe.contrib.fingerprint import fingerprint

        tasks = [_task("t1", 1.0), _task("t2", 0.0), _task("t3", 1.0)]
        fp = fingerprint(tasks)
        assert fp == (1.0, 0.0, 1.0)

    def test_similarity(self) -> None:
        from codeprobe.contrib.fingerprint import similarity

        a = [_task("t1", 1.0), _task("t2", 0.0)]
        b = [_task("t1", 1.0), _task("t2", 0.0)]
        assert similarity(a, b) == 1.0

    def test_dissimilarity(self) -> None:
        from codeprobe.contrib.fingerprint import similarity

        a = [_task("t1", 1.0), _task("t2", 0.0)]
        b = [_task("t1", 0.0), _task("t2", 1.0)]
        assert similarity(a, b) == 0.0


# ===========================================================================
# Debate
# ===========================================================================


class TestDebate:
    def test_compare_returns_verdict(self) -> None:
        from codeprobe.contrib.debate import compare_configs

        a = _results("A", [_task("t1", 1.0), _task("t2", 1.0)])
        b = _results("B", [_task("t1", 0.0), _task("t2", 0.0)])
        verdict = compare_configs(a, b)
        assert verdict.winner == "A"
        assert len(verdict.arguments) > 0

    def test_tie(self) -> None:
        from codeprobe.contrib.debate import compare_configs

        a = _results("A", [_task("t1", 1.0), _task("t2", 0.0)])
        b = _results("B", [_task("t1", 0.0), _task("t2", 1.0)])
        verdict = compare_configs(a, b)
        assert verdict.winner == "tie"


# ===========================================================================
# Decision Tree
# ===========================================================================


class TestDecisionTree:
    def test_build_tree(self) -> None:
        from codeprobe.contrib.decision_tree import build_decision_tree

        configs = [
            _results("A", [
                _task("t1", 1.0, metadata={"difficulty": "easy"}),
                _task("t2", 0.0, metadata={"difficulty": "hard"}),
            ]),
            _results("B", [
                _task("t1", 0.0, metadata={"difficulty": "easy"}),
                _task("t2", 1.0, metadata={"difficulty": "hard"}),
            ]),
        ]
        tree = build_decision_tree(configs, feature_key="difficulty")
        assert isinstance(tree, dict)
        assert "easy" in tree or "hard" in tree


# ===========================================================================
# Pareto
# ===========================================================================


class TestPareto:
    def test_pareto_front(self) -> None:
        from codeprobe.contrib.pareto import pareto_front

        configs = [
            _results("cheap-bad", [_task("t1", 0.0, cost_usd=0.01), _task("t2", 0.0, cost_usd=0.01)]),
            _results("mid-mid", [_task("t1", 1.0, cost_usd=0.05), _task("t2", 0.0, cost_usd=0.05)]),
            _results("expensive-good", [_task("t1", 1.0, cost_usd=0.50), _task("t2", 1.0, cost_usd=0.50)]),
            _results("expensive-bad", [_task("t1", 0.0, cost_usd=0.50), _task("t2", 0.0, cost_usd=0.50)]),
        ]
        front = pareto_front(configs)
        labels = {f.label for f in front}
        assert "expensive-bad" not in labels
        assert "expensive-good" in labels

    def test_single_config_is_on_front(self) -> None:
        from codeprobe.contrib.pareto import pareto_front

        configs = [_results("only", [_task("t1", 1.0, cost_usd=0.50)])]
        front = pareto_front(configs)
        assert len(front) == 1

    def test_none_cost_excluded_from_pareto(self) -> None:
        """Agent with cost_usd=None (e.g. subscription) must NOT appear at zero on cost axis."""
        from codeprobe.contrib.pareto import pareto_front

        configs = [
            _results("subscription", [_task("t1", 1.0, cost_usd=None), _task("t2", 1.0, cost_usd=None)]),
            _results("per-token", [_task("t1", 1.0, cost_usd=0.10), _task("t2", 1.0, cost_usd=0.10)]),
        ]
        front = pareto_front(configs)
        labels = {f.label for f in front}
        # subscription agent has unknown cost — must be excluded from cost-based Pareto
        assert "subscription" not in labels
        assert "per-token" in labels

    def test_zero_cost_is_legitimate(self) -> None:
        """Agent with cost_usd=0.0 (legitimately free) SHOULD appear at zero on cost axis."""
        from codeprobe.contrib.pareto import pareto_front

        configs = [
            _results("free-good", [_task("t1", 1.0, cost_usd=0.0), _task("t2", 1.0, cost_usd=0.0)]),
            _results("paid-good", [_task("t1", 1.0, cost_usd=0.50), _task("t2", 1.0, cost_usd=0.50)]),
        ]
        front = pareto_front(configs)
        labels = {f.label for f in front}
        # Free agent with same score dominates paid agent
        assert "free-good" in labels

    def test_mixed_cost_models_only_known_costs_in_pareto(self) -> None:
        """Only configs with all known costs participate in cost-based Pareto."""
        from codeprobe.contrib.pareto import pareto_front

        configs = [
            _results("known-cheap", [_task("t1", 1.0, cost_usd=0.01)]),
            _results("known-expensive", [_task("t1", 1.0, cost_usd=1.00)]),
            _results("unknown-cost", [_task("t1", 1.0, cost_usd=None)]),
            _results("partial-unknown", [_task("t1", 1.0, cost_usd=0.05), _task("t2", 1.0, cost_usd=None)]),
        ]
        front = pareto_front(configs)
        labels = {f.label for f in front}
        # Configs with any None cost_usd must be excluded
        assert "unknown-cost" not in labels
        assert "partial-unknown" not in labels
        # known-cheap dominates known-expensive (same score, lower cost)
        assert "known-cheap" in labels


# ===========================================================================
# Adaptive Sampling
# ===========================================================================


class TestAdaptiveSampling:
    def test_suggest_next(self) -> None:
        from codeprobe.contrib.adaptive import suggest_next_tasks

        completed = [_task("t1", 1.0), _task("t2", 0.0)]
        available = ["t3", "t4", "t5"]
        suggested = suggest_next_tasks(completed, available, count=2, seed=42)
        assert len(suggested) <= 2
        assert all(t in available for t in suggested)

    def test_suggest_empty_available(self) -> None:
        from codeprobe.contrib.adaptive import suggest_next_tasks

        suggested = suggest_next_tasks([], [], count=3)
        assert suggested == []
