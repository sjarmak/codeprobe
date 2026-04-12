"""Acceptance criteria manifest and loader.

This package contains the machine-readable manifest of acceptance criteria
derived from codeprobe PRDs (``docs/prd/*``) along with a loader that parses,
validates, filters, and topologically sorts criteria for execution by the
acceptance loop runner.

Public API:

- :class:`Criterion` — frozen dataclass describing a single criterion
- :func:`load_criteria` — parse and validate ``criteria.toml``
- :func:`filter_by_tier` — filter criteria by tier
- :func:`filter_by_severity` — filter criteria by severity
- :func:`topological_sort` — DAG-order criteria by ``depends_on``

Tiers:

- ``structural`` — source-level assertions (constants, types, file layout)
- ``behavioral`` — runtime assertions (CLI exit codes, log content, IO)
- ``statistical`` — aggregate assertions over a run's outputs
"""

from __future__ import annotations

from acceptance.converge import (
    BLOCKING_SEVERITIES,
    DEFAULT_MAX_ITERATIONS,
    ConvergenceController,
    Decision,
    DecisionResult,
)
from acceptance.loader import (
    ALLOWED_SEVERITIES,
    ALLOWED_TIERS,
    REQUIRED_FIELDS,
    Criterion,
    filter_by_severity,
    filter_by_tier,
    load_criteria,
    topological_sort,
)

__all__ = [
    "ALLOWED_SEVERITIES",
    "ALLOWED_TIERS",
    "BLOCKING_SEVERITIES",
    "DEFAULT_MAX_ITERATIONS",
    "REQUIRED_FIELDS",
    "ConvergenceController",
    "Criterion",
    "Decision",
    "DecisionResult",
    "filter_by_severity",
    "filter_by_tier",
    "load_criteria",
    "topological_sort",
]
