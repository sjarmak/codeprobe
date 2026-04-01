"""Task data model — represents a single eval task mined from a repo."""

from __future__ import annotations

from dataclasses import dataclass, field


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
    tags: tuple[str, ...] = ()
    estimated_duration_sec: int = 300
    resource_tier: str = "medium"


@dataclass(frozen=True)
class TaskVerification:
    """How to verify task completion."""

    type: str = "test_script"
    command: str = "bash tests/test.sh"
    reward_type: str = "binary"


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
