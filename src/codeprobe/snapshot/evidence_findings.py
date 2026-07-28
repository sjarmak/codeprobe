"""Mechanical rendering for the bounded evidence findings artifact."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

FINDINGS_SCHEMA = "codeprobe.zero-code-access.findings.v1"


def _display(value: object) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value).lower() if isinstance(value, bool) else str(value)


def render_findings(aggregate: Mapping[str, Any]) -> str:
    """Render the fixed, bounded findings artifact from validated aggregates."""
    configurations = aggregate["configurations"]
    comparison = aggregate["comparison"]
    warnings = aggregate["validity_warnings"]
    warning_text = ", ".join(warnings) if warnings else "none"
    rows = "\n".join(
        "| "
        + " | ".join(
            (
                str(item["configuration_id"]),
                _display(item["mean_quality"]),
                _display(item["total_cost_usd"]),
                _display(item["mean_latency_seconds"]),
                str(item["scorable_run_count"]),
            )
        )
        + " |"
        for item in configurations
    )
    return (
        "---\n"
        f"schema_version: {FINDINGS_SCHEMA}\n"
        f"approval_digest: {aggregate['approval_digest']}\n"
        f"conclusion: {aggregate['conclusion']}\n"
        f"evidence_sufficient: {_display(aggregate['evidence_sufficient'])}\n"
        "---\n"
        "# Zero-Code-Access Findings\n\n"
        f"- Conclusion: `{aggregate['conclusion']}`\n"
        f"- Paired distinct tasks: {aggregate['paired_task_count']}\n"
        f"- Repeats per task and configuration: {aggregate['repeats_per_task']}\n"
        f"- Validity warnings: `{warning_text}`\n\n"
        "## Aggregate configuration results\n\n"
        "| Configuration | Mean quality | Total cost USD | "
        "Mean latency seconds | Scorable runs |\n"
        "| --- | ---: | ---: | ---: | ---: |\n"
        f"{rows}\n\n"
        "## Aggregate comparison\n\n"
        f"- Score difference: {_display(comparison['score_difference'])}\n"
        f"- Cost difference USD: {_display(comparison['cost_difference_usd'])}\n"
        "- Latency difference seconds: "
        f"{_display(comparison['latency_difference_seconds'])}\n"
        f"- P-value: {_display(comparison['p_value'])}\n"
        f"- Effect size: {_display(comparison['effect_size'])}\n"
        f"- Effect-size method: `{comparison['effect_size_method']}`\n"
    )
