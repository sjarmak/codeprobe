"""Tests for the tool-surface utilization audit (codeprobe-1gg).

Covers the four acceptance criteria:
  A1 — an arm with zero calls into an enabled surface is flagged.
  A2 — "agent declined" (zero calls, ran) is distinguished from
       "infra made the tool fail" (errors present).
  A3 — surface policies are derived from config (covered here +
       tests/lint/test_tool_surface_policy.py).
  A4 — diagnostic only; reward/score untouched (the audit reads
       tool_use_by_name, never automated_score).
"""

from __future__ import annotations

from codeprobe.core.tool_surface_audit import (
    ToolSurfacePolicy,
    audit_tool_surface_usage,
    derive_surface_policies,
    task_abandoned_any_surface,
)
from codeprobe.models.experiment import CompletedTask, ExperimentConfig

_SG_MCP_CONFIG = {"mcpServers": {"sourcegraph": {"command": "sg-mcp"}}}


def _task(
    *,
    tool_use_by_name: dict[str, int] | None,
    status: str = "completed",
    error_category: str | None = None,
    score: float = 1.0,
) -> CompletedTask:
    return CompletedTask(
        task_id="t",
        automated_score=score,
        status=status,
        error_category=error_category,
        tool_use_by_name=tool_use_by_name,
    )


def _sg_config(**kwargs) -> ExperimentConfig:
    return ExperimentConfig(label="with-sg", mcp_config=_SG_MCP_CONFIG, **kwargs)


class TestDeriveSurfacePolicies:
    def test_derives_server_from_mcp_config(self) -> None:
        policies = derive_surface_policies(_sg_config())
        assert policies == [
            ToolSurfacePolicy(surface="sourcegraph", prefixes=("mcp__sourcegraph",))
        ]

    def test_derives_server_from_allowed_tools(self) -> None:
        config = ExperimentConfig(
            label="x",
            allowed_tools=["Write", "mcp__sourcegraph__keyword_search"],
        )
        policies = derive_surface_policies(config)
        assert policies == [
            ToolSurfacePolicy(surface="sourcegraph", prefixes=("mcp__sourcegraph",))
        ]

    def test_local_only_config_has_no_surface(self) -> None:
        config = ExperimentConfig(label="local-only", allowed_tools=["Read", "Bash"])
        assert derive_surface_policies(config) == []

    def test_dedupes_servers_from_both_sources(self) -> None:
        config = ExperimentConfig(
            label="x",
            mcp_config=_SG_MCP_CONFIG,
            allowed_tools=["mcp__sourcegraph__read_file"],
        )
        assert len(derive_surface_policies(config)) == 1

    def test_policy_matches_tools_under_prefix(self) -> None:
        policy = derive_surface_policies(_sg_config())[0]
        assert policy.matches("mcp__sourcegraph__keyword_search")
        assert not policy.matches("Read")


class TestAbandonmentDetection:
    def test_a1_zero_calls_flags_abandonment(self) -> None:
        # Agent ran but never called the enabled SG surface — the 9tk blind
        # spot, now caught mechanically.
        task = _task(tool_use_by_name={"Read": 3, "Bash": 2})
        findings = audit_tool_surface_usage(task, _sg_config())
        assert len(findings) == 1
        assert findings[0].abandoned is True
        assert findings[0].reason == "zero-calls"
        assert task_abandoned_any_surface(task, _sg_config())

    def test_surface_used_is_not_abandoned(self) -> None:
        task = _task(
            tool_use_by_name={"mcp__sourcegraph__keyword_search": 2, "Read": 1}
        )
        findings = audit_tool_surface_usage(task, _sg_config())
        assert findings[0].abandoned is False
        assert findings[0].calls == 2
        assert findings[0].reason == "used"

    def test_local_only_never_flagged(self) -> None:
        config = ExperimentConfig(label="local-only", allowed_tools=["Read"])
        task = _task(tool_use_by_name={"Read": 5})
        assert audit_tool_surface_usage(task, config) == []
        assert not task_abandoned_any_surface(task, config)


class TestInfraFailureDistinction:
    def test_a2_infra_error_is_not_abandonment(self) -> None:
        # status='error' is an infra casualty — the agent never got a fair
        # shot at the surface, so zero calls is NOT abandonment.
        task = _task(tool_use_by_name={"Read": 1}, status="error")
        finding = audit_tool_surface_usage(task, _sg_config())[0]
        assert finding.abandoned is False
        assert finding.reason == "infra-failure"

    def test_a2_quota_failure_is_not_abandonment(self) -> None:
        task = _task(
            tool_use_by_name={}, status="error", error_category="quota"
        )
        finding = audit_tool_surface_usage(task, _sg_config())[0]
        assert finding.abandoned is False
        assert finding.reason == "infra-failure"

    def test_terminal_agent_failure_still_counts_as_abandonment(self) -> None:
        # status='failed' (e.g. max_turns) means the agent DID run to a stop
        # condition; declining the surface across that run is abandonment.
        task = _task(tool_use_by_name={"Read": 10}, status="failed")
        finding = audit_tool_surface_usage(task, _sg_config())[0]
        assert finding.abandoned is True


class TestUsageNotCaptured:
    def test_none_usage_is_never_abandonment(self) -> None:
        # Can't prove zero calls when the transcript wasn't captured — must
        # not assert abandonment (no false positives).
        task = _task(tool_use_by_name=None)
        finding = audit_tool_surface_usage(task, _sg_config())[0]
        assert finding.abandoned is False
        assert finding.reason == "usage-not-captured"


class TestSummaryWiring:
    """abandoned_surface_count flows through the analysis summarizers."""

    def test_summarize_config_counts_abandoned_trials(self) -> None:
        from codeprobe.analysis.stats import summarize_config
        from codeprobe.models.experiment import ConfigResults

        results = ConfigResults(
            config="with-sg",
            completed=[
                _task(tool_use_by_name={"Read": 2}),  # abandoned
                _task(tool_use_by_name={"mcp__sourcegraph__keyword_search": 1}),
                _task(tool_use_by_name={"Bash": 1}, status="error"),  # infra
            ],
        )
        summary = summarize_config(results, config=_sg_config())
        assert summary.abandoned_surface_count == 1

    def test_summary_zero_without_config(self) -> None:
        # No config supplied → the audit can't run; count stays 0 rather
        # than guessing (A4: purely additive diagnostic).
        from codeprobe.analysis.stats import summarize_config
        from codeprobe.models.experiment import ConfigResults

        results = ConfigResults(
            config="with-sg",
            completed=[_task(tool_use_by_name={"Read": 2})],
        )
        assert summarize_config(results).abandoned_surface_count == 0

    def test_streaming_summary_matches(self) -> None:
        from codeprobe.analysis.stats import summarize_completed_tasks

        tasks = [
            _task(tool_use_by_name={"Read": 2}),
            _task(tool_use_by_name={"mcp__sourcegraph__find_references": 3}),
        ]
        summary = summarize_completed_tasks(
            "with-sg", iter(tasks), config=_sg_config()
        )
        assert summary.abandoned_surface_count == 1

    def test_report_text_surfaces_warning(self) -> None:
        from codeprobe.analysis.report import format_text_report, generate_report
        from codeprobe.models.experiment import ConfigResults

        results = [
            ConfigResults(
                config="with-sg",
                completed=[_task(tool_use_by_name={"Read": 2})],
            )
        ]
        report = generate_report(
            "exp", results, configs=[_sg_config()]
        )
        text = format_text_report(report)
        assert "abandoned-surface" in text
        assert "INVALID" in text
