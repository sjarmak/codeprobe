"""Tests for the TOML/JSON task loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeprobe.loaders import load_task
from codeprobe.models.task import Task

# -- TOML fixtures ----------------------------------------------------------

CCX_TOML = """\
version = "1.0"

[metadata]
name = "CCX-sgauth-301"
difficulty = "hard"
description = "Security Compliance Audit"
license = "Apache-2.0"

[task]
id = "CCX-sgauth-301"
name = "CCX-sgauth-301"
repo = "sourcegraph/sourcegraph"
category = "compliance-audit"
language = "go"
difficulty = "hard"
time_limit_sec = 900
mcp_suite = "csb_org_compliance"
org_scale = true
verification_modes = ["artifact"]

[verification]
type = "test"
command = "bash /tests/test.sh"
reward_type = "checkpoint"
description = "Security Compliance Audit verification"
"""

MINED_TOML = """\
[metadata]
name = "sg-deepsearch-anchor-fix-001"
difficulty = "hard"
category = "fix"
language = "TypeScript"
tags = ["sourcegraph", "mined", "TypeScript"]

[task]
id = "sg-deepsearch-anchor-fix-001"
name = "sg-deepsearch-anchor-fix-001"
time_limit_sec = 1800
repo = "sourcegraph/sourcegraph"
category = "csb_sdlc_fix"

[verification]
reward_type = "test_ratio"
description = "Fix anchor detection"
"""

MINIMAL_TOML = """\
[task]
id = "minimal-001"
repo = "org/repo"

[metadata]
name = "minimal"
"""


# -- JSON fixture (legacy metadata.json format) -----------------------------

LEGACY_JSON = {
    "id": "t-001",
    "repo": "org/repo",
    "metadata": {
        "name": "test-task",
        "difficulty": "medium",
        "description": "A test task",
    },
    "verification": {
        "type": "test_script",
        "command": "bash tests/test.sh",
        "reward_type": "binary",
    },
    "time_limit_sec": 300,
}


# -- Tests -------------------------------------------------------------------


class TestLoadTomlCcxFormat:
    def test_loads_core_fields(self, tmp_path: Path) -> None:
        p = tmp_path / "task.toml"
        p.write_text(CCX_TOML)
        task = load_task(p)

        assert isinstance(task, Task)
        assert task.id == "CCX-sgauth-301"
        assert task.repo == "sourcegraph/sourcegraph"
        assert task.time_limit_sec == 900

    def test_loads_metadata(self, tmp_path: Path) -> None:
        p = tmp_path / "task.toml"
        p.write_text(CCX_TOML)
        task = load_task(p)

        assert task.metadata.name == "CCX-sgauth-301"
        assert task.metadata.difficulty == "hard"
        assert task.metadata.description == "Security Compliance Audit"
        assert task.metadata.language == "go"
        assert task.metadata.org_scale is True
        assert task.metadata.mcp_suite == "csb_org_compliance"

    def test_loads_verification(self, tmp_path: Path) -> None:
        p = tmp_path / "task.toml"
        p.write_text(CCX_TOML)
        task = load_task(p)

        assert task.verification.reward_type == "checkpoint"
        assert task.verification.type == "test"
        assert task.verification.command == "bash /tests/test.sh"

    def test_loads_verification_modes(self, tmp_path: Path) -> None:
        p = tmp_path / "task.toml"
        p.write_text(CCX_TOML)
        task = load_task(p)

        assert task.verification_modes == ("artifact",)


class TestLoadTomlMinedFormat:
    def test_loads_core_fields(self, tmp_path: Path) -> None:
        p = tmp_path / "task.toml"
        p.write_text(MINED_TOML)
        task = load_task(p)

        assert task.id == "sg-deepsearch-anchor-fix-001"
        assert task.repo == "sourcegraph/sourcegraph"
        assert task.time_limit_sec == 1800

    def test_loads_tags(self, tmp_path: Path) -> None:
        p = tmp_path / "task.toml"
        p.write_text(MINED_TOML)
        task = load_task(p)

        assert task.metadata.tags == ("sourcegraph", "mined", "TypeScript")

    def test_test_ratio_reward_type(self, tmp_path: Path) -> None:
        p = tmp_path / "task.toml"
        p.write_text(MINED_TOML)
        task = load_task(p)

        assert task.verification.reward_type == "test_ratio"

    def test_defaults_for_missing_fields(self, tmp_path: Path) -> None:
        p = tmp_path / "task.toml"
        p.write_text(MINED_TOML)
        task = load_task(p)

        assert task.metadata.org_scale is False
        assert task.metadata.mcp_suite is None
        assert task.verification_modes == ()


class TestLoadTomlMinimal:
    def test_minimal_toml_loads(self, tmp_path: Path) -> None:
        p = tmp_path / "task.toml"
        p.write_text(MINIMAL_TOML)
        task = load_task(p)

        assert task.id == "minimal-001"
        assert task.repo == "org/repo"
        assert task.metadata.name == "minimal"
        assert task.verification.reward_type == "binary"


class TestLoadJson:
    def test_loads_legacy_json(self, tmp_path: Path) -> None:
        p = tmp_path / "metadata.json"
        p.write_text(json.dumps(LEGACY_JSON))
        task = load_task(p)

        assert isinstance(task, Task)
        assert task.id == "t-001"
        assert task.repo == "org/repo"
        assert task.metadata.name == "test-task"
        assert task.verification.reward_type == "binary"
        assert task.time_limit_sec == 300


class TestLoadTaskValidation:
    def test_unknown_extension_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "task.yaml"
        p.write_text("key: value")
        with pytest.raises(ValueError, match="Unsupported file extension"):
            load_task(p)

    def test_unknown_reward_type_raises(self, tmp_path: Path) -> None:
        bad_toml = """\
[task]
id = "bad-001"
repo = "org/repo"

[metadata]
name = "bad"

[verification]
reward_type = "magic"
"""
        p = tmp_path / "task.toml"
        p.write_text(bad_toml)
        with pytest.raises(ValueError, match="Unknown reward_type"):
            load_task(p)

    def test_missing_task_id_raises(self, tmp_path: Path) -> None:
        bad_toml = """\
[task]
repo = "org/repo"

[metadata]
name = "no-id"
"""
        p = tmp_path / "task.toml"
        p.write_text(bad_toml)
        with pytest.raises(ValueError, match="Missing required field 'id'"):
            load_task(p)

    def test_missing_task_section_raises(self, tmp_path: Path) -> None:
        bad_toml = """\
[metadata]
name = "no-task-section"
"""
        p = tmp_path / "task.toml"
        p.write_text(bad_toml)
        with pytest.raises(ValueError, match="Missing required \\[task\\] section"):
            load_task(p)

    def test_task_is_frozen(self, tmp_path: Path) -> None:
        p = tmp_path / "task.toml"
        p.write_text(CCX_TOML)
        task = load_task(p)
        with pytest.raises(AttributeError):
            task.id = "mutated"  # type: ignore[misc]
