"""Regression tests for positive-reward exit-code fallback detection."""

from tests.lint.scorer_honesty_exit_fallback import (
    find_positive_reward_exit_fallbacks,
)


def test_lint_catches_positive_reward_exit_fallback() -> None:
    """A composite verifier cannot award full credit from exit status alone."""
    bad_source = (
        "class CheckpointScorer:\n"
        "    def _run_verifier(self, run):\n"
        "        if run.returncode == 0:\n"
        "            return 1.0, 'subprocess', False\n"
        "        return 0.0, 'subprocess', False\n"
    )
    findings = find_positive_reward_exit_fallbacks(
        bad_source, "synthetic.py"
    )
    assert [finding.rule for finding in findings] == [
        "positive-reward-exit-fallback"
    ]


def test_lint_allows_positive_reward_after_stdout_contract() -> None:
    """Parsed verifier stdout makes the legacy empty-output fallback explicit."""
    good_source = (
        "class CheckpointScorer:\n"
        "    def _run_verifier(self, run):\n"
        "        parsed, invalid = _parse_composite_verifier_stdout(\n"
        "            run.stdout, 'checkpoint'\n"
        "        )\n"
        "        if parsed is not None:\n"
        "            return parsed, 'subprocess', invalid\n"
        "        if run.returncode == 0:\n"
        "            return 1.0, 'subprocess', False\n"
        "        return 0.0, 'subprocess', False\n"
    )
    assert (
        find_positive_reward_exit_fallbacks(good_source, "synthetic.py")
        == []
    )


def test_lint_does_not_reclassify_binary_scorer_contract() -> None:
    """BinaryScorer intentionally maps process success directly to reward."""
    source = (
        "class BinaryScorer:\n"
        "    def score(self, run):\n"
        "        if run.returncode == 0:\n"
        "            return ScoreResult(score=1.0)\n"
        "        return ScoreResult(score=0.0)\n"
    )
    assert find_positive_reward_exit_fallbacks(source, "synthetic.py") == []


def test_lint_rejects_ignored_stdout_parse() -> None:
    """Calling the parser without gating the fallback does not satisfy it."""
    source = (
        "class CheckpointScorer:\n"
        "    def _run_verifier(self, run):\n"
        "        _parse_composite_verifier_stdout(run.stdout, 'checkpoint')\n"
        "        if run.returncode == 0:\n"
        "            return 1.0, 'subprocess', False\n"
    )
    findings = find_positive_reward_exit_fallbacks(source, "synthetic.py")
    assert [finding.rule for finding in findings] == [
        "positive-reward-exit-fallback"
    ]


def test_lint_resolves_named_positive_reward_constant() -> None:
    """A named literal cannot hide positive reward from the AST rule."""
    source = (
        "FULL_CREDIT = 1.0\n"
        "class CheckpointScorer:\n"
        "    def _run_verifier(self, run):\n"
        "        if run.returncode == 0:\n"
        "            return FULL_CREDIT, 'subprocess', False\n"
    )
    findings = find_positive_reward_exit_fallbacks(source, "synthetic.py")
    assert [finding.rule for finding in findings] == [
        "positive-reward-exit-fallback"
    ]


def test_lint_ignores_positive_return_in_nested_function() -> None:
    """Nested helper returns are not returns from the scorer method."""
    source = (
        "class CheckpointScorer:\n"
        "    def _run_verifier(self, run):\n"
        "        if run.returncode == 0:\n"
        "            def nested():\n"
        "                return 1.0\n"
        "            return 0.0, 'subprocess', False\n"
    )
    assert find_positive_reward_exit_fallbacks(source, "synthetic.py") == []


def test_lint_rejects_conditional_return_inside_parsed_guard() -> None:
    """The parsed-score guard must not fall through to exit-only credit."""
    source = (
        "class CheckpointScorer:\n"
        "    def _run_verifier(self, run, trust_parsed):\n"
        "        parsed, invalid = _parse_composite_verifier_stdout(\n"
        "            run.stdout, 'checkpoint'\n"
        "        )\n"
        "        if parsed is not None:\n"
        "            if trust_parsed:\n"
        "                return parsed, 'subprocess', invalid\n"
        "        if run.returncode == 0:\n"
        "            return 1.0, 'subprocess', False\n"
    )
    findings = find_positive_reward_exit_fallbacks(source, "synthetic.py")
    assert [finding.rule for finding in findings] == [
        "positive-reward-exit-fallback"
    ]


def test_lint_resolves_method_local_positive_reward_constant() -> None:
    """A method-local literal alias cannot hide exit-only positive reward."""
    source = (
        "class CheckpointScorer:\n"
        "    def _run_verifier(self, run):\n"
        "        full_credit = 1.0\n"
        "        if run.returncode == 0:\n"
        "            return full_credit, 'subprocess', False\n"
    )
    findings = find_positive_reward_exit_fallbacks(source, "synthetic.py")
    assert [finding.rule for finding in findings] == [
        "positive-reward-exit-fallback"
    ]
