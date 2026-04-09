"""Task data model — represents a single eval task mined from a repo."""

from __future__ import annotations

from dataclasses import dataclass, field

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
    oracle_type: str = ""  # "file_list", "count", "boolean" — empty for SDLC
    oracle_answer: tuple[str, ...] = ()  # expected answer set for oracle tasks
    oracle_tiers: dict[str, str] = field(
        default_factory=dict
    )  # file→tier mapping: "required"|"supplementary"|"context"
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
