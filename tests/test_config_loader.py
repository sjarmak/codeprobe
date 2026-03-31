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

    def test_explicit_config_extracts_reward_type(self) -> None:
        """reward_type is extracted from explicit configs."""
        evalrc = EvalrcConfig(
            name="reward",
            configs={
                "continuous": {"agent": "claude", "reward_type": "continuous"},
            },
        )
        experiment = to_experiment(evalrc)
        assert experiment.configs[0].reward_type == "continuous"

    def test_explicit_config_reward_type_defaults_to_binary(self) -> None:
        """reward_type defaults to binary when not specified."""
        evalrc = EvalrcConfig(
            name="default-reward",
            configs={"baseline": {"agent": "claude"}},
        )
        experiment = to_experiment(evalrc)
        assert experiment.configs[0].reward_type == "binary"


class TestDimensions:
    """Test dimensions-based cross-product config generation."""

    def test_dimensions_models_only(self) -> None:
        """Single models axis produces one config per model."""
        evalrc = EvalrcConfig(
            name="dim-models",
            dimensions={"models": {"sonnet": "claude-sonnet-4-6", "opus": "claude-opus-4-6"}},
        )
        experiment = to_experiment(evalrc)
        assert len(experiment.configs) == 2
        labels = {c.label for c in experiment.configs}
        assert labels == {"sonnet", "opus"}
        sonnet = next(c for c in experiment.configs if c.label == "sonnet")
        assert sonnet.model == "claude-sonnet-4-6"

    def test_dimensions_cross_product(self) -> None:
        """models × tools × prompts produces full cross-product."""
        evalrc = EvalrcConfig(
            name="dim-cross",
            dimensions={
                "models": {"sonnet": "claude-sonnet-4-6"},
                "tools": {
                    "baseline": None,
                    "with-sg": {"sourcegraph": {"command": "npx"}},
                },
                "prompts": {"default": "instruction.md", "mcp": "instruction_mcp.md"},
            },
        )
        experiment = to_experiment(evalrc)
        # 1 model × 2 tools × 2 prompts = 4 configs
        assert len(experiment.configs) == 4
        labels = {c.label for c in experiment.configs}
        assert "baseline-default" in labels
        assert "baseline-mcp" in labels
        assert "with-sg-default" in labels
        assert "with-sg-mcp" in labels

    def test_dimensions_prompts_as_instruction_variant(self) -> None:
        """String prompt values set instruction_variant."""
        evalrc = EvalrcConfig(
            name="dim-prompts",
            dimensions={
                "models": {"sonnet": "claude-sonnet-4-6"},
                "prompts": {"mcp": "instruction_mcp.md"},
            },
        )
        experiment = to_experiment(evalrc)
        assert len(experiment.configs) == 1
        assert experiment.configs[0].instruction_variant == "instruction_mcp.md"

    def test_dimensions_prompts_as_preamble_list(self) -> None:
        """List prompt values set preambles tuple."""
        evalrc = EvalrcConfig(
            name="dim-preambles",
            dimensions={
                "models": {"sonnet": "claude-sonnet-4-6"},
                "prompts": {"guided": ["tdd", "sourcegraph"]},
            },
        )
        experiment = to_experiment(evalrc)
        assert len(experiment.configs) == 1
        assert experiment.configs[0].preambles == ("tdd", "sourcegraph")

    def test_dimensions_single_value_axis_omitted_from_label(self) -> None:
        """Axes with a single value are omitted from the label."""
        evalrc = EvalrcConfig(
            name="dim-label",
            dimensions={
                "models": {"sonnet": "claude-sonnet-4-6"},
                "tools": {"baseline": None, "with-sg": {"sg": {}}},
            },
        )
        experiment = to_experiment(evalrc)
        # models has 1 entry, tools has 2 — label uses only tool axis
        labels = {c.label for c in experiment.configs}
        assert labels == {"baseline", "with-sg"}

    def test_dimensions_overrides_legacy_matrix(self) -> None:
        """dimensions takes priority over legacy agents × models matrix."""
        evalrc = EvalrcConfig(
            name="dim-override",
            agents=["claude", "copilot"],
            models=["sonnet", "opus"],
            dimensions={"models": {"haiku": "claude-haiku-4-5"}},
        )
        experiment = to_experiment(evalrc)
        # Should use dimensions (1 config), not legacy matrix (4 configs)
        assert len(experiment.configs) == 1
        assert experiment.configs[0].label == "haiku"

    def test_dimensions_empty_is_noop(self) -> None:
        """Empty dimensions dict falls through to legacy resolution."""
        evalrc = EvalrcConfig(
            name="dim-empty",
            agents=["claude"],
            models=["sonnet"],
            dimensions={},
        )
        experiment = to_experiment(evalrc)
        # Should use legacy matrix
        assert len(experiment.configs) == 1
        assert experiment.configs[0].label == "claude-sonnet"

    def test_dimensions_tools_sets_mcp_config(self) -> None:
        """Tools dimension values set mcp_config on ExperimentConfig."""
        evalrc = EvalrcConfig(
            name="dim-tools",
            dimensions={
                "models": {"sonnet": "claude-sonnet-4-6"},
                "tools": {"with-sg": {"sourcegraph": {"command": "npx"}}},
            },
        )
        experiment = to_experiment(evalrc)
        assert experiment.configs[0].mcp_config == {"sourcegraph": {"command": "npx"}}

    def test_dimensions_unknown_axis_raises(self) -> None:
        """Unknown dimension axis names raise ValueError."""
        evalrc = EvalrcConfig(
            name="dim-unknown",
            dimensions={"model": {"sonnet": "claude-sonnet-4-6"}},  # typo: "model" not "models"
        )
        with pytest.raises(ValueError, match="Unknown dimension axes"):
            to_experiment(evalrc)

    def test_dimensions_uses_first_agent(self) -> None:
        """Dimensions configs use the first agent from the evalrc."""
        evalrc = EvalrcConfig(
            name="dim-agent",
            agents=["copilot", "claude"],
            dimensions={"models": {"sonnet": "claude-sonnet-4-6"}},
        )
        experiment = to_experiment(evalrc)
        assert experiment.configs[0].agent == "copilot"
