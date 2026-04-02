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

__all__ = [
    "ConfigSummary",
    "PairwiseComparison",
    "RankedConfig",
    "Report",
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
