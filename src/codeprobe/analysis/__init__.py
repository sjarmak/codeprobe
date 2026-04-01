"""Analysis — run interpretation, statistical comparison, reporting."""

from codeprobe.analysis.ranking import RankedConfig, rank_configs
from codeprobe.analysis.report import (
    Report,
    format_json_report,
    format_text_report,
    generate_report,
    generate_report_streaming,
)
from codeprobe.analysis.stats import (
    ConfigSummary,
    PairwiseComparison,
    compare_configs,
    summarize_completed_tasks,
    summarize_config,
)

__all__ = [
    "ConfigSummary",
    "PairwiseComparison",
    "RankedConfig",
    "Report",
    "compare_configs",
    "format_json_report",
    "format_text_report",
    "generate_report",
    "generate_report_streaming",
    "rank_configs",
    "summarize_completed_tasks",
    "summarize_config",
]
