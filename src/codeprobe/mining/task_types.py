"""Task-type registry — maps codeprobe task types to user-facing metadata.

Adding a new task type is purely a data change: insert a ``TaskTypeInfo``
entry into :data:`TASK_TYPE_REGISTRY`. Dispatch code looks up the pipeline
key via :attr:`TaskTypeInfo.dispatch_key`, so no ``if/elif`` chain expands
when new types are added.

The canonical set of valid types is also enforced by
:data:`codeprobe.models.task.TASK_TYPES`; this module extends each type
with a human-readable description and the CodeScaleBench suite it maps
to (verified against ``benchmarks/suites/csb-v2-dual264.json``).

Primary user entry points:

* :func:`list_task_types` — iterable of ``(name, info)`` for CLI display
* :func:`get_task_type` — lookup by name with ``KeyError`` on miss
* :func:`task_type_names` — sorted list of valid names for click.Choice
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(frozen=True)
class TaskTypeInfo:
    """Metadata for a single task type.

    Attributes:
        name: The task_type identifier (matches
            :data:`codeprobe.models.task.TASK_TYPES`, with ``"mixed"`` added
            as a meta-type).
        description: User-facing description (>= 40 chars).
        csb_suite: Primary CodeScaleBench suite identifier (must exist in
            ``benchmarks/suites/csb-v2-dual264.json``).
        csb_suites: All CSB suites this task type maps to (the primary
            suite plus related alternates). Order is significant for
            display.
        dispatch_key: Internal pipeline identifier consumed by
            ``_dispatch_by_task_type``. One of ``"sdlc"``, ``"probe"``,
            ``"comprehension"``, ``"org_scale"``, ``"mixed"``.
    """

    name: str
    description: str
    csb_suite: str
    csb_suites: tuple[str, ...]
    dispatch_key: str

    def __post_init__(self) -> None:
        if len(self.description) < 40:
            raise ValueError(
                f"TaskTypeInfo.description for {self.name!r} must be "
                f">= 40 chars (got {len(self.description)})"
            )
        if self.csb_suite not in self.csb_suites:
            raise ValueError(
                f"TaskTypeInfo.csb_suite {self.csb_suite!r} must appear in "
                f"csb_suites {self.csb_suites!r}"
            )


TASK_TYPE_REGISTRY: dict[str, TaskTypeInfo] = {
    "sdlc_code_change": TaskTypeInfo(
        name="sdlc_code_change",
        description=(
            "Code-change tasks mined from merged PRs: feature work, bug "
            "fixes, refactors. Scored via PR-derived test scripts."
        ),
        csb_suite="csb_sdlc_fix",
        csb_suites=(
            "csb_sdlc_fix",
            "csb_sdlc_feature",
            "csb_sdlc_refactor",
            "csb_sdlc_debug",
        ),
        dispatch_key="sdlc",
    ),
    "micro_probe": TaskTypeInfo(
        name="micro_probe",
        description=(
            "Single-symbol micro-benchmarks that probe an agent's ability "
            "to navigate and comprehend code without writing changes."
        ),
        csb_suite="csb_sdlc_understand",
        csb_suites=("csb_sdlc_understand",),
        dispatch_key="probe",
    ),
    "mcp_tool_usage": TaskTypeInfo(
        name="mcp_tool_usage",
        description=(
            "Harder cross-file / cross-repo tasks biased toward MCP tool "
            "benefit (aliased imports, blast radius, type hierarchies)."
        ),
        csb_suite="csb_org_crossrepo_tracing",
        csb_suites=(
            "csb_org_crossrepo_tracing",
            "csb_org_crossrepo",
            "csb_org_crossorg",
        ),
        dispatch_key="sdlc",
    ),
    "architecture_comprehension": TaskTypeInfo(
        name="architecture_comprehension",
        description=(
            "Architecture comprehension tasks: explain module responsibilities, "
            "trace request flows, identify extension points."
        ),
        csb_suite="csb_org_onboarding",
        csb_suites=(
            "csb_org_onboarding",
            "csb_org_domain",
            "csb_sdlc_document",
        ),
        dispatch_key="comprehension",
    ),
    "org_scale_cross_repo": TaskTypeInfo(
        name="org_scale_cross_repo",
        description=(
            "Org-scale comprehension tasks over multi-repo pattern families: "
            "compliance audits, migration inventories, incident debug."
        ),
        csb_suite="csb_org_crossrepo",
        csb_suites=(
            "csb_org_crossrepo",
            "csb_org_compliance",
            "csb_org_migration",
            "csb_org_incident",
            "csb_org_platform",
            "csb_org_security",
        ),
        dispatch_key="org_scale",
    ),
    "dependency_upgrade": TaskTypeInfo(
        name="dependency_upgrade",
        description=(
            "Dependency upgrade tasks mined from merged PRs that bump "
            "package manifests or lockfiles to a newer version. Structural "
            "filter: diff touches only dependency manifest files."
        ),
        csb_suite="csb_sdlc_fix",
        csb_suites=(
            "csb_sdlc_fix",
            "csb_sdlc_refactor",
        ),
        dispatch_key="sdlc",
    ),
    "mixed": TaskTypeInfo(
        name="mixed",
        description=(
            "Balanced mix of SDLC code-change and micro-probe comprehension "
            "tasks for a general-purpose benchmark."
        ),
        csb_suite="csb_sdlc_fix",
        csb_suites=(
            "csb_sdlc_fix",
            "csb_sdlc_understand",
            "csb_sdlc_feature",
        ),
        dispatch_key="mixed",
    ),
}


def task_type_names() -> list[str]:
    """Return sorted list of registered task-type names (for click.Choice)."""
    return sorted(TASK_TYPE_REGISTRY.keys())


def get_task_type(name: str) -> TaskTypeInfo:
    """Look up a registered task type by name.

    Raises:
        KeyError: if *name* is not registered. Callers turning this into
            a user-facing error should catch and format the valid set via
            :func:`task_type_names`.
    """
    return TASK_TYPE_REGISTRY[name]


def list_task_types() -> Iterator[tuple[str, TaskTypeInfo]]:
    """Iterate ``(name, info)`` pairs in a stable display order."""
    for name in task_type_names():
        yield name, TASK_TYPE_REGISTRY[name]


__all__ = [
    "TaskTypeInfo",
    "TASK_TYPE_REGISTRY",
    "task_type_names",
    "get_task_type",
    "list_task_types",
]
