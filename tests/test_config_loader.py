"""Tests for .evalrc.yaml config loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from codeprobe.config.loader import load_evalrc, to_experiment
from codeprobe.models.evalrc import EvalrcConfig
from codeprobe.models.experiment import Experiment


class TestLoadEvalrc:
    """Test loading .evalrc.yaml files."""

    def test_load_minimal(self, tmp_path: Path) -> None:
        (tmp_path / ".evalrc.yaml").write_text("name: my-exp\n")
        config = load_evalrc(tmp_path)
        assert config.name == "my-exp"
        assert config.agents == ["claude"]
        assert config.tasks_dir == "tasks"

    def test_load_full(self, tmp_path: Path) -> None:
        yaml_content = (
            "name: full-test\n"
            "description: A full experiment\n"
            "tasks_dir: my-tasks\n"
            "agents: [claude, copilot]\n"
            "models: [claude-sonnet-4-6, claude-opus-4-6]\n"
            "configs:\n"
            "  baseline:\n"
            "    agent: claude\n"
            "    model: claude-sonnet-4-6\n"
            "  upgraded:\n"
            "    agent: claude\n"
            "    model: claude-opus-4-6\n"
        )
        (tmp_path / ".evalrc.yaml").write_text(yaml_content)
        config = load_evalrc(tmp_path)
        assert config.name == "full-test"
        assert config.description == "A full experiment"
        assert config.tasks_dir == "my-tasks"
        assert config.agents == ["claude", "copilot"]
        assert config.models == ["claude-sonnet-4-6", "claude-opus-4-6"]
        assert "baseline" in config.configs
        assert config.configs["baseline"]["model"] == "claude-sonnet-4-6"

    def test_load_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_evalrc(tmp_path)

    def test_load_invalid_yaml(self, tmp_path: Path) -> None:
        (tmp_path / ".evalrc.yaml").write_text(": : invalid\n\t[broken")
        with pytest.raises(ValueError, match="Invalid .evalrc.yaml"):
            load_evalrc(tmp_path)

    def test_load_empty_file(self, tmp_path: Path) -> None:
        (tmp_path / ".evalrc.yaml").write_text("")
        with pytest.raises(ValueError, match="Invalid .evalrc.yaml"):
            load_evalrc(tmp_path)

    def test_load_evalrc_yml_fallback(self, tmp_path: Path) -> None:
        """Should also find .evalrc.yml (without 'a')."""
        (tmp_path / ".evalrc.yml").write_text("name: alt-ext\n")
        config = load_evalrc(tmp_path)
        assert config.name == "alt-ext"

    def test_yaml_takes_precedence_over_yml(self, tmp_path: Path) -> None:
        (tmp_path / ".evalrc.yaml").write_text("name: yaml-wins\n")
        (tmp_path / ".evalrc.yml").write_text("name: yml-loses\n")
        config = load_evalrc(tmp_path)
        assert config.name == "yaml-wins"

    def test_defaults_applied(self, tmp_path: Path) -> None:
        (tmp_path / ".evalrc.yaml").write_text("name: defaults-test\n")
        config = load_evalrc(tmp_path)
        assert config.description == ""
        assert config.models == []
        assert config.configs == {}

    def test_scalar_config_entry_raises(self, tmp_path: Path) -> None:
        """Config entries must be mappings, not scalars."""
        yaml_content = (
            "name: bad-config\n"
            "configs:\n"
            "  baseline: claude\n"
        )
        (tmp_path / ".evalrc.yaml").write_text(yaml_content)
        config = load_evalrc(tmp_path)
        with pytest.raises(ValueError, match="must be a mapping"):
            to_experiment(config)

    def test_manual_fallback_without_pyyaml(self, tmp_path: Path) -> None:
        """Config loads when PyYAML is absent (flat keys only)."""
        from unittest.mock import patch

        (tmp_path / ".evalrc.yaml").write_text(
            "name: fallback-test\nagents: [claude, copilot]\n"
        )
        with patch.dict("sys.modules", {"yaml": None}):
            config = load_evalrc(tmp_path)
        assert config.name == "fallback-test"
        assert config.agents == ["claude", "copilot"]


class TestToExperiment:
    """Test conversion from EvalrcConfig to Experiment."""

    def test_basic_conversion(self) -> None:
        evalrc = EvalrcConfig(
            name="test-exp",
            description="Test experiment",
            tasks_dir="my-tasks",
            agents=["claude"],
            models=["claude-sonnet-4-6"],
        )
        experiment = to_experiment(evalrc)
        assert isinstance(experiment, Experiment)
        assert experiment.name == "test-exp"
        assert experiment.description == "Test experiment"
        assert experiment.tasks_dir == "my-tasks"

    def test_model_matrix_generates_configs(self) -> None:
        """agents x models produces experiment configs."""
        evalrc = EvalrcConfig(
            name="matrix",
            agents=["claude", "copilot"],
            models=["sonnet", "opus"],
        )
        experiment = to_experiment(evalrc)
        assert len(experiment.configs) == 4
        labels = {c.label for c in experiment.configs}
        assert labels == {"claude-sonnet", "claude-opus", "copilot-sonnet", "copilot-opus"}

    def test_explicit_configs_override_matrix(self) -> None:
        """When configs dict is provided, use those instead of matrix."""
        evalrc = EvalrcConfig(
            name="explicit",
            agents=["claude"],
            models=["sonnet"],
            configs={
                "baseline": {"agent": "claude", "model": "sonnet"},
                "upgraded": {"agent": "claude", "model": "opus", "permission_mode": "auto"},
            },
        )
        experiment = to_experiment(evalrc)
        assert len(experiment.configs) == 2
        labels = {c.label for c in experiment.configs}
        assert labels == {"baseline", "upgraded"}
        upgraded = next(c for c in experiment.configs if c.label == "upgraded")
        assert upgraded.model == "opus"
        assert upgraded.permission_mode == "auto"

    def test_single_agent_no_models(self) -> None:
        """Single agent with no models produces one config."""
        evalrc = EvalrcConfig(name="simple", agents=["claude"])
        experiment = to_experiment(evalrc)
        assert len(experiment.configs) == 1
        assert experiment.configs[0].label == "claude"
        assert experiment.configs[0].agent == "claude"

    def test_multiple_agents_no_models(self) -> None:
        """Multiple agents with no models produces one config per agent."""
        evalrc = EvalrcConfig(name="multi", agents=["claude", "copilot"])
        experiment = to_experiment(evalrc)
        assert len(experiment.configs) == 2
        labels = {c.label for c in experiment.configs}
        assert labels == {"claude", "copilot"}
