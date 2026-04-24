"""Unit tests for codeprobe.config.defaults.

Each resolver is exercised with the feature flag ON and OFF via
parametrization. The resolvers themselves are pure — ``use_v07_defaults()``
is the only function that reads the environment — so the tests below
primarily verify the resolver contract (``(value, source)`` shape +
correct priority rules + PrescriptiveError raising).
"""

from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

import pytest

from codeprobe.config.defaults import (
    CODEPROBE_DEFAULTS_ENV,
    PrescriptiveError,
    RepoShape,
    compact_budget_bytes,
    resolve_enrich,
    resolve_experiment_config,
    resolve_goal,
    resolve_max_cost_usd,
    resolve_mcp_families,
    resolve_narrative_source,
    resolve_out_calibrate,
    resolve_preamble,
    resolve_sg_repo,
    resolve_suite,
    resolve_task_type,
    resolve_timeout,
    scan_repo_shape,
    use_v07_defaults,
)

# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env_value,expected",
    [
        ("", False),
        ("v0.6", False),
        ("v0.7", True),
        ("V0.7", False),  # case-sensitive; typo tolerance falls through to v0.6
        ("random", False),
    ],
)
def test_use_v07_defaults(
    monkeypatch: pytest.MonkeyPatch, env_value: str, expected: bool
) -> None:
    if env_value:
        monkeypatch.setenv(CODEPROBE_DEFAULTS_ENV, env_value)
    else:
        monkeypatch.delenv(CODEPROBE_DEFAULTS_ENV, raising=False)

    assert use_v07_defaults() is expected


# ---------------------------------------------------------------------------
# RepoShape + scan_repo_shape
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path, *, commits: int = 0, merge: bool = False) -> None:
    subprocess.run(
        ["git", "init", "-q", str(path)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "test"],
        check=True,
    )

    for i in range(commits):
        (path / f"f{i}.txt").write_text(f"content {i}\n")
        subprocess.run(
            ["git", "-C", str(path), "add", "."],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "commit", "-q", "-m", f"commit {i}"],
            check=True,
            capture_output=True,
        )

    if merge and commits > 0:
        # Create a branch with a commit, then merge it with --no-ff so
        # git log --merges finds the merge commit.
        subprocess.run(
            ["git", "-C", str(path), "checkout", "-q", "-b", "feature"],
            check=True,
            capture_output=True,
        )
        (path / "feature.txt").write_text("feature\n")
        subprocess.run(
            ["git", "-C", str(path), "add", "."], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(path), "commit", "-q", "-m", "feature commit"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "checkout", "-q", "master"],
            check=False,
            capture_output=True,
        )
        # Fall back to main branch if master doesn't exist.
        subprocess.run(
            ["git", "-C", str(path), "checkout", "-q", "main"],
            check=False,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(path),
                "merge",
                "--no-ff",
                "-q",
                "-m",
                "merge feature",
                "feature",
            ],
            check=False,
            capture_output=True,
        )


def test_scan_repo_shape_empty(tmp_path: Path) -> None:
    # Non-git directory — all fields default to 0 / False.
    shape = scan_repo_shape(tmp_path)
    assert shape.repo_path == tmp_path.resolve()
    assert shape.commit_count == 0
    assert shape.has_merged_prs is False
    assert shape.pr_density == 0.0


def test_scan_repo_shape_commits_only(tmp_path: Path) -> None:
    _init_git_repo(tmp_path, commits=2, merge=False)
    shape = scan_repo_shape(tmp_path)
    assert shape.commit_count >= 2
    assert shape.has_merged_prs is False


def test_scan_repo_shape_with_merge(tmp_path: Path) -> None:
    _init_git_repo(tmp_path, commits=1, merge=True)
    shape = scan_repo_shape(tmp_path)
    assert shape.commit_count >= 2
    assert shape.has_merged_prs is True


# ---------------------------------------------------------------------------
# resolve_goal
# ---------------------------------------------------------------------------


def test_resolve_goal_prefers_mcp_when_config_present(tmp_path: Path) -> None:
    shape = RepoShape(repo_path=tmp_path, has_mcp_config=True, commit_count=10)
    goal, source = resolve_goal(shape)
    assert goal == "mcp"
    assert source == "auto-detected"


def test_resolve_goal_quality_when_prs_exist(tmp_path: Path) -> None:
    shape = RepoShape(
        repo_path=tmp_path,
        has_merged_prs=True,
        commit_count=10,
        pr_density=0.3,
    )
    goal, source = resolve_goal(shape)
    assert goal == "quality"
    assert source == "auto-detected"


def test_resolve_goal_general_when_only_commits(tmp_path: Path) -> None:
    shape = RepoShape(repo_path=tmp_path, commit_count=5)
    goal, source = resolve_goal(shape)
    assert goal == "general"


def test_resolve_goal_undetectable(tmp_path: Path) -> None:
    shape = RepoShape(repo_path=tmp_path)
    with pytest.raises(PrescriptiveError) as exc_info:
        resolve_goal(shape)
    assert exc_info.value.code == "GOAL_UNDETECTABLE"
    assert exc_info.value.next_try_flag == "--goal"


# ---------------------------------------------------------------------------
# resolve_task_type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "goal,expected",
    [
        ("quality", "sdlc_code_change"),
        ("navigation", "architecture_comprehension"),
        ("mcp", "mcp_tool_usage"),
        ("general", "mixed"),
        ("unknown", "mixed"),
    ],
)
def test_resolve_task_type(goal: str, expected: str) -> None:
    value, source = resolve_task_type(goal)
    assert value == expected
    assert source == "auto-detected"


# ---------------------------------------------------------------------------
# resolve_narrative_source
# ---------------------------------------------------------------------------


def test_resolve_narrative_source_pr_priority(tmp_path: Path) -> None:
    shape = RepoShape(
        repo_path=tmp_path,
        has_merged_prs=True,
        commit_count=10,
        has_rfcs=True,
    )
    value, source = resolve_narrative_source(shape)
    assert value == ("pr",)
    assert source == "auto-detected"


def test_resolve_narrative_source_commits_fallback(tmp_path: Path) -> None:
    shape = RepoShape(
        repo_path=tmp_path,
        has_merged_prs=False,
        commit_count=5,
        has_rfcs=True,
    )
    value, _ = resolve_narrative_source(shape)
    assert value == ("commits",)


def test_resolve_narrative_source_rfcs_fallback(tmp_path: Path) -> None:
    shape = RepoShape(
        repo_path=tmp_path,
        has_merged_prs=False,
        commit_count=0,
        has_rfcs=True,
    )
    value, _ = resolve_narrative_source(shape)
    assert value == ("rfcs",)


def test_resolve_narrative_source_undetectable(tmp_path: Path) -> None:
    shape = RepoShape(repo_path=tmp_path)
    with pytest.raises(PrescriptiveError) as exc_info:
        resolve_narrative_source(shape)
    assert exc_info.value.code == "NARRATIVE_SOURCE_UNDETECTABLE"
    assert exc_info.value.next_try_flag == "--narrative-source"
    assert exc_info.value.next_try_value == "commits"


# ---------------------------------------------------------------------------
# resolve_enrich
# ---------------------------------------------------------------------------


def test_resolve_enrich_llm_available() -> None:
    value, source = resolve_enrich(True)
    assert value is True
    assert source == "llm-available"


def test_resolve_enrich_llm_missing() -> None:
    value, source = resolve_enrich(False)
    assert value is False
    assert source == "default"


# ---------------------------------------------------------------------------
# resolve_mcp_families
# ---------------------------------------------------------------------------


def test_resolve_mcp_families_all_three_signals() -> None:
    value, source = resolve_mcp_families(
        "mcp",
        {"SOURCEGRAPH_TOKEN": "x"},
        "sourcegraph",
    )
    assert value is True
    assert source == "auto-detected"


def test_resolve_mcp_families_missing_token() -> None:
    value, source = resolve_mcp_families("mcp", {}, "sourcegraph")
    assert value is False
    assert source == "default"


def test_resolve_mcp_families_wrong_goal() -> None:
    value, _ = resolve_mcp_families(
        "quality", {"SOURCEGRAPH_TOKEN": "x"}, "sourcegraph"
    )
    assert value is False


# ---------------------------------------------------------------------------
# resolve_sg_repo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "remote,expected",
    [
        ("https://github.com/numpy/numpy.git", "github.com/sg-evals/numpy"),
        ("git@github.com:pytorch/pytorch.git", "github.com/sg-evals/pytorch"),
        ("", ""),
        (None, ""),
    ],
)
def test_resolve_sg_repo(remote: str | None, expected: str) -> None:
    value, source = resolve_sg_repo(remote)
    assert value == expected
    if expected:
        assert source == "auto-detected"
    else:
        assert source == "default"


# ---------------------------------------------------------------------------
# resolve_max_cost_usd / resolve_timeout
# ---------------------------------------------------------------------------


def test_resolve_max_cost_usd() -> None:
    value, source = resolve_max_cost_usd()
    assert value == 10.00
    assert source == "default"


def test_resolve_timeout_mcp() -> None:
    value, source = resolve_timeout("mcp")
    assert value == 3600
    assert source == "auto-detected"


def test_resolve_timeout_quality() -> None:
    value, source = resolve_timeout("quality")
    assert value == 600
    assert source == "default"


# ---------------------------------------------------------------------------
# resolve_experiment_config / resolve_suite
# ---------------------------------------------------------------------------


def test_resolve_experiment_config_single_match(tmp_path: Path) -> None:
    (tmp_path / ".codeprobe").mkdir()
    exp_json = tmp_path / ".codeprobe" / "experiment.json"
    exp_json.write_text("{}")
    value, source = resolve_experiment_config(tmp_path)
    assert value == exp_json
    assert source == "config-file"


def test_resolve_experiment_config_zero_matches(tmp_path: Path) -> None:
    with pytest.raises(PrescriptiveError) as exc_info:
        resolve_experiment_config(tmp_path)
    assert exc_info.value.code == "AMBIGUOUS_EXPERIMENT"
    assert exc_info.value.next_try_flag == "--config"


def test_resolve_suite_single_match(tmp_path: Path) -> None:
    suite = tmp_path / "suite.toml"
    suite.write_text("")
    value, source = resolve_suite(tmp_path)
    assert value == suite
    assert source == "config-file"


def test_resolve_suite_no_matches(tmp_path: Path) -> None:
    with pytest.raises(PrescriptiveError) as exc_info:
        resolve_suite(tmp_path)
    assert exc_info.value.code == "AMBIGUOUS_EXPERIMENT"
    assert exc_info.value.next_try_flag == "--suite"


# ---------------------------------------------------------------------------
# resolve_out_calibrate
# ---------------------------------------------------------------------------


def test_resolve_out_calibrate_deterministic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    value, source = resolve_out_calibrate("v1", now=date(2026, 4, 23))
    assert value == tmp_path / "calibration_v1_20260423.json"
    assert source == "default"


# ---------------------------------------------------------------------------
# resolve_preamble
# ---------------------------------------------------------------------------


def test_resolve_preamble_none_input() -> None:
    value, source = resolve_preamble(None)
    assert value == "generic"
    assert source == "default"


def test_resolve_preamble_custom_priority(tmp_path: Path) -> None:
    codeprobe_dir = tmp_path / ".codeprobe"
    codeprobe_dir.mkdir()
    (codeprobe_dir / "preamble.md").write_text("")
    (tmp_path / ".github").mkdir()
    value, source = resolve_preamble(tmp_path)
    assert value == "custom"
    assert source == "auto-detected"


def test_resolve_preamble_github_when_no_custom(tmp_path: Path) -> None:
    (tmp_path / ".github").mkdir()
    value, _ = resolve_preamble(tmp_path)
    assert value == "github"


# ---------------------------------------------------------------------------
# Cross-cutting cap
# ---------------------------------------------------------------------------


def test_compact_budget_bytes() -> None:
    assert compact_budget_bytes() == 2048


# ---------------------------------------------------------------------------
# Flag-off parity: calling resolvers under v0.6/unset should still work
# (they are pure functions — v0.6 just means callers skip them).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag_value", ["", "v0.6", "v0.7"])
def test_resolvers_pure_under_any_flag_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag_value: str
) -> None:
    if flag_value:
        monkeypatch.setenv(CODEPROBE_DEFAULTS_ENV, flag_value)
    else:
        monkeypatch.delenv(CODEPROBE_DEFAULTS_ENV, raising=False)

    shape = RepoShape(
        repo_path=tmp_path,
        has_merged_prs=True,
        commit_count=5,
        pr_density=0.2,
    )
    goal, _ = resolve_goal(shape)
    assert goal == "quality"
    ns, _ = resolve_narrative_source(shape)
    assert ns == ("pr",)
