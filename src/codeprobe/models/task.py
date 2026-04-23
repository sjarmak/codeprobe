"""Task data model — represents a single eval task mined from a repo."""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "ORG_SCALE_CATEGORIES",
    "TASK_TYPES",
    "VERIFICATION_MODES",
    "RepoRef",
    "TaskMetadata",
    "Checkpoint",
    "TaskVerification",
    "Task",
]


@dataclass(frozen=True)
class RepoRef:
    """Reference to a secondary repository needed by a cross-repo task.

    Canonical definition — imported by both ``mining.multi_repo`` and
    ``core.isolation``.
    """

    name: str
    ground_truth_commit: str
    url: str = ""  # empty when ``local_path`` is provided
    local_path: str = ""  # absolute path to a local clone to copy

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("RepoRef.name must be non-empty")
        if not self.ground_truth_commit:
            raise ValueError("RepoRef.ground_truth_commit must be non-empty")
        if not self.url and not self.local_path:
            raise ValueError("RepoRef requires either url or local_path")


# Valid org-scale task family categories.
ORG_SCALE_CATEGORIES: frozenset[str] = frozenset(
    {
        "migration-inventory",
        "compliance-audit",
        "cross-repo-dep-trace",
        "cross-repo-config-trace",
        "onboarding-comprehension",
        "incident-debug",
        "platform-knowledge",
        "domain-lineage",
    }
)

# Valid task type categories (added in Layer 0 for benchmark taxonomy).
TASK_TYPES: frozenset[str] = frozenset(
    {
        "sdlc_code_change",
        "micro_probe",
        "mcp_tool_usage",
        "architecture_comprehension",
        "org_scale_cross_repo",
        "dependency_upgrade",
    }
)

# Valid verification modes for the taxonomy layer.
VERIFICATION_MODES: frozenset[str] = frozenset(
    {
        "test_script",
        "artifact_eval",
        "dual",
    }
)


@dataclass(frozen=True)
class TaskMetadata:
    """Metadata about a task's origin and characteristics."""

    name: str
    difficulty: str = "medium"
    description: str = ""
    license: str = ""
    language: str = ""
    category: str = "sdlc"
    org_scale: bool = False
    mcp_suite: str | None = None
    task_type: str = "sdlc_code_change"
    tags: tuple[str, ...] = ()
    estimated_duration_sec: int = 300
    resource_tier: str = "medium"
    issue_title: str = ""
    issue_body: str = ""
    quality_score: float = 0.0
    enrichment_source: str = ""
    ground_truth_commit: str = ""
    ground_truth_commits: tuple[
        tuple[str, str], ...
    ] = ()  # (repo_name, sha) pairs for multi-repo
    sg_repo: str = ""  # Sourcegraph repo identifier for MCP instruction variant
    # Secondary repos required to complete the task (cross-repo mining).
    # Empty tuple for single-repo tasks (backwards compatible).
    additional_repos: tuple[RepoRef, ...] = ()
    # Expected tool-use benefit for this task, populated by a curator model
    # call at mine time (ZFC-compliant — the string is a delegated judgment,
    # not a hardcoded heuristic). Accepted values: "" | "low" | "medium" | "high".
    # Empty string means "not assessed" (e.g. offline mining without an LLM).
    expected_tool_benefit: str = ""
    # Plain-text rationale emitted alongside expected_tool_benefit by the same
    # curator call. Empty when expected_tool_benefit is empty.
    tool_benefit_rationale: str = ""
    # Snapshot of MCP CAPABILITIES registry keys active at mine time. Tuple
    # (not list) so TaskMetadata remains hashable. Enables drift detection —
    # see ``codeprobe check-infra --fail-on-capability-drift``.
    mcp_capabilities_at_mine_time: tuple[str, ...] = ()


@dataclass(frozen=True)
class Checkpoint:
    """A single weighted checkpoint verifier defined in task metadata.

    Mirrors the EnterpriseBench ``[[checkpoints]]`` TOML pattern.
    """

    name: str
    weight: float
    verifier: str
    description: str = ""


@dataclass(frozen=True)
class TaskVerification:
    """How to verify task completion."""

    type: str = "test_script"
    command: str = "bash tests/test.sh"
    verification_mode: str = "test_script"
    eval_command: str = ""
    ground_truth_path: str = "tests/ground_truth.json"
    answer_schema: str = ""
    reward_type: str = "binary"
    scoring_policy: str = ""
    weight_direct: float = 0.5
    weight_artifact: float = 0.5
    oracle_type: str = ""  # "file_list", "count", "boolean" — empty for SDLC
    oracle_answer: tuple[str, ...] = ()  # expected answer set for oracle tasks
    oracle_tiers: tuple[
        tuple[str, str], ...
    ] = ()  # file→tier mapping: "required"|"supplementary"|"context"
    ground_truth_schema_version: str = ""
    checkpoints: tuple[Checkpoint, ...] = ()  # from [[checkpoints]] in task.toml


@dataclass(frozen=True)
class Task:
    """A single eval task with instruction, verification, and metadata."""

    id: str
    repo: str
    metadata: TaskMetadata
    verification: TaskVerification = field(default_factory=TaskVerification)
    instruction_path: str = "instruction.md"
    instruction_variant_path: str | None = None
    time_limit_sec: int = 300
    verification_modes: tuple[str, ...] = ()
