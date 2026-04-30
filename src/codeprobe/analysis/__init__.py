"""Analysis — run interpretation, statistical comparison, reporting."""

from codeprobe.analysis.ranking import RankedConfig, rank_configs
from codeprobe.analysis.report import (
    Report,
    format_csv_report,
    format_html_report,
    format_json_report,
    format_text_report,
    generate_report,
    generate_report_streaming,
)
from codeprobe.analysis.stats import (
    ConfigSummary,
    PairwiseComparison,
    cliffs_delta,
    cohens_d,
    compare_configs,
    mcnemars_exact_test,
    summarize_completed_tasks,
    summarize_config,
    wilcoxon_test,
    wilson_ci,
)
from codeprobe.analysis.trace_quality import (
    LOW_RECALL_THRESHOLD,
    SCHEMA_VERSION as TRACE_QUALITY_SCHEMA_VERSION,
    TraceQualityMetrics,
    TraceQualityReporter,
    TraceQualitySummary,
)

__all__ = [
    "ConfigSummary",
    "LOW_RECALL_THRESHOLD",
    "PairwiseComparison",
    "RankedConfig",
    "Report",
    "TRACE_QUALITY_SCHEMA_VERSION",
    "TraceQualityMetrics",
    "TraceQualityReporter",
    "TraceQualitySummary",
    "format_csv_report",
    "format_html_report",
    "cliffs_delta",
    "cohens_d",
    "compare_configs",
    "format_json_report",
    "format_text_report",
    "generate_report",
    "generate_report_streaming",
    "mcnemars_exact_test",
    "rank_configs",
    "summarize_completed_tasks",
    "summarize_config",
    "wilcoxon_test",
    "wilson_ci",
]
