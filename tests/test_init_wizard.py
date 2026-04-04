"""Tests for the interactive init wizard."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from codeprobe.cli import main
from codeprobe.cli.wizard import (
    ask_custom,
    ask_mcp_comparison,
    ask_model_comparison,
    ask_prompt_comparison,
    validate_experiment_name,
)
from codeprobe.cli.yaml_writer import write_evalrc
from codeprobe.models.evalrc import EvalrcConfig

# ---------------------------------------------------------------------------
# yaml_writer tests
# ---------------------------------------------------------------------------


class TestWriteEvalrc:
    """Tests for .evalrc.yaml serialization."""

    def test_writes_file(self, tmp_path: Path) -> None:
        config = EvalrcConfig(name="test-exp", agents=["claude"])
        result = write_evalrc(tmp_path, config)
        assert result.exists()
        assert result.name == ".evalrc.yaml"

    def test_content_has_name(self, tmp_path: Path) -> None:
        config = EvalrcConfig(name="my-experiment", agents=["claude", "copilot"])
        result = write_evalrc(tmp_path, config)
        content = result.read_text()
        assert "my-experiment" in content
        assert "claude" in content
        assert "copilot" in content

    def test_omits_defaults(self, tmp_path: Path) -> None:
        config = EvalrcConfig(name="minimal")
        result = write_evalrc(tmp_path, config)
        content = result.read_text()
        # Should not include empty description or empty models list
        assert "description" not in content

    def test_includes_models_when_set(self, tmp_path: Path) -> None:
        config = EvalrcConfig(
            name="model-test",
            agents=["claude"],
            models=["claude-sonnet-4-6", "claude-opus-4-6"],
        )
        result = write_evalrc(tmp_path, config)
        content = result.read_text()
        assert "claude-sonnet-4-6" in content
        assert "claude-opus-4-6" in content

    def test_fallback_without_pyyaml(self, tmp_path: Path) -> None:
        config = EvalrcConfig(name="fallback-test", agents=["claude"])
        with patch.dict("sys.modules", {"yaml": None}):
            result = write_evalrc(tmp_path, config)
        assert result.exists()
        content = result.read_text()
        assert "fallback-test" in content


# ---------------------------------------------------------------------------
# wizard tests (goal-specific questionnaire functions)
# ---------------------------------------------------------------------------


class TestValidation:
    """Tests for input validation."""

    def test_valid_experiment_name(self) -> None:
        assert validate_experiment_name("my-experiment") == "my-experiment"
        assert validate_experiment_name("test_123") == "test_123"
        assert validate_experiment_name("v0.1.0") == "v0.1.0"

    def test_invalid_experiment_name_path_traversal(self) -> None:
        import click

        with pytest.raises(click.BadParameter):
            validate_experiment_name("../../../etc")

    def test_invalid_experiment_name_slash(self) -> None:
        import click

        with pytest.raises(click.BadParameter):
            validate_experiment_name("foo/bar")

    def test_load_json_missing_file(self, tmp_path: Path) -> None:
        import click

        from codeprobe.cli.wizard import _load_json

        with pytest.raises(click.BadParameter, match="File not found"):
            _load_json(str(tmp_path / "nonexistent.json"))

    def test_load_json_invalid_json(self, tmp_path: Path) -> None:
        import click

        from codeprobe.cli.wizard import _load_json

        bad_file = tmp_path / "bad.json"
        bad_file.write_text("not json")
        with pytest.raises(click.BadParameter, match="Invalid JSON"):
            _load_json(str(bad_file))

    def test_load_json_not_dict(self, tmp_path: Path) -> None:
        import click

        from codeprobe.cli.wizard import _load_json

        arr_file = tmp_path / "array.json"
        arr_file.write_text("[1, 2, 3]")
        with pytest.raises(click.BadParameter, match="Expected a JSON object"):
            _load_json(str(arr_file))


class TestAskMcpComparison:
    """Goal 1: Compare baseline vs MCP-augmented agent."""

    def test_produces_two_configs(self, tmp_path: Path) -> None:
        mcp_file = tmp_path / "mcp.json"
        mcp_file.write_text(json.dumps({"mcpServers": {}}))

        evalrc, configs = ask_mcp_comparison(
            experiment_name="mcp-comparison",
            agent="claude",
            model=None,
            mcp_config_path=str(mcp_file),
        )
        assert len(configs) == 2
        labels = {c.label for c in configs}
        assert "baseline" in labels
        assert "with-mcp" in labels

    def test_mcp_config_attached(self, tmp_path: Path) -> None:
        mcp_data = {"mcpServers": {"test": {"command": "echo"}}}
        mcp_file = tmp_path / "mcp.json"
        mcp_file.write_text(json.dumps(mcp_data))

        evalrc, configs = ask_mcp_comparison(
            experiment_name="mcp-test",
            agent="claude",
            model=None,
            mcp_config_path=str(mcp_file),
        )
        mcp_config = next(c for c in configs if c.label == "with-mcp")
        assert mcp_config.mcp_config == mcp_data

    def test_baseline_has_no_mcp(self, tmp_path: Path) -> None:
        mcp_file = tmp_path / "mcp.json"
        mcp_file.write_text(json.dumps({"mcpServers": {}}))

        evalrc, configs = ask_mcp_comparison(
            experiment_name="test",
            agent="claude",
            model=None,
            mcp_config_path=str(mcp_file),
        )
        baseline = next(c for c in configs if c.label == "baseline")
        assert baseline.mcp_config is None


class TestAskModelComparison:
    """Goal 2: Compare different models."""

    def test_produces_config_per_model(self) -> None:
        evalrc, configs = ask_model_comparison(
            experiment_name="model-comparison",
            agent="claude",
            models=["claude-sonnet-4-6", "claude-opus-4-6"],
        )
        assert len(configs) == 2
        assert configs[0].model == "claude-sonnet-4-6"
        assert configs[1].model == "claude-opus-4-6"

    def test_labels_from_model_names(self) -> None:
        evalrc, configs = ask_model_comparison(
            experiment_name="test",
            agent="claude",
            models=["claude-sonnet-4-6", "claude-opus-4-6"],
        )
        labels = [c.label for c in configs]
        assert "claude-sonnet-4-6" in labels
        assert "claude-opus-4-6" in labels

    def test_evalrc_has_models(self) -> None:
        evalrc, configs = ask_model_comparison(
            experiment_name="test",
            agent="claude",
            models=["claude-sonnet-4-6", "claude-opus-4-6"],
        )
        assert evalrc.models == ["claude-sonnet-4-6", "claude-opus-4-6"]


class TestAskPromptComparison:
    """Goal 3: Compare different prompts/instruction styles."""

    def test_produces_config_per_variant(self) -> None:
        evalrc, configs = ask_prompt_comparison(
            experiment_name="prompt-comparison",
            agent="claude",
            model=None,
            variants=["prompts/concise.md", "prompts/detailed.md"],
        )
        assert len(configs) == 2

    def test_instruction_variant_set(self) -> None:
        evalrc, configs = ask_prompt_comparison(
            experiment_name="test",
            agent="claude",
            model="claude-sonnet-4-6",
            variants=["prompts/concise.md", "prompts/detailed.md"],
        )
        assert configs[0].instruction_variant == "prompts/concise.md"
        assert configs[1].instruction_variant == "prompts/detailed.md"

    def test_labels_from_variant_stems(self) -> None:
        evalrc, configs = ask_prompt_comparison(
            experiment_name="test",
            agent="claude",
            model=None,
            variants=["prompts/concise.md", "prompts/detailed.md"],
        )
        labels = [c.label for c in configs]
        assert "concise" in labels
        assert "detailed" in labels


class TestAskCustom:
    """Goal 4: Custom comparison."""

    def test_produces_correct_count(self) -> None:
        configs_input = [
            {"label": "fast", "agent": "claude", "model": "claude-sonnet-4-6"},
            {"label": "deep", "agent": "claude", "model": "claude-opus-4-6"},
        ]
        evalrc, configs = ask_custom(
            experiment_name="custom-exp",
            configs=configs_input,
        )
        assert len(configs) == 2

    def test_config_fields_populated(self) -> None:
        configs_input = [
            {"label": "fast", "agent": "copilot", "model": "gpt-4o"},
        ]
        evalrc, configs = ask_custom(
            experiment_name="custom",
            configs=configs_input,
        )
        assert configs[0].label == "fast"
        assert configs[0].agent == "copilot"
        assert configs[0].model == "gpt-4o"


# ---------------------------------------------------------------------------
# Integration: full CLI flow via CliRunner
# ---------------------------------------------------------------------------


class TestInitCliIntegration:
    """End-to-end tests via CliRunner."""

    def test_goal1_mcp_flow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mcp_file = tmp_path / "mcp.json"
        mcp_file.write_text(json.dumps({"mcpServers": {}}))

        # Patch discovery so the prompt falls through to manual path entry
        monkeypatch.setattr("codeprobe.cli.init_cmd._discover_mcp_configs", lambda: [])

        runner = CliRunner()
        # Inputs: goal=1, experiment name (enter=default), agent (enter=default),
        # model (enter=skip), mcp config path
        input_text = f"1\n\nclaude\n\n{mcp_file}\n"
        result = runner.invoke(main, ["init", str(tmp_path)], input=input_text)
        assert result.exit_code == 0, result.output
        assert (tmp_path / ".evalrc.yaml").exists()
        assert (tmp_path / ".codeprobe").is_dir()

    def test_goal2_model_flow(self, tmp_path: Path) -> None:
        runner = CliRunner()
        input_text = "2\n\nclaude\nclaude-sonnet-4-6, claude-opus-4-6\n"
        result = runner.invoke(main, ["init", str(tmp_path)], input=input_text)
        assert result.exit_code == 0, result.output
        assert (tmp_path / ".evalrc.yaml").exists()

    def test_goal3_prompt_flow(self, tmp_path: Path) -> None:
        runner = CliRunner()
        input_text = "3\n\nclaude\n\nprompts/a.md, prompts/b.md\n"
        result = runner.invoke(main, ["init", str(tmp_path)], input=input_text)
        assert result.exit_code == 0, result.output
        assert (tmp_path / ".evalrc.yaml").exists()

    def test_prints_next_steps(self, tmp_path: Path) -> None:
        runner = CliRunner()
        input_text = "2\n\nclaude\nclaude-sonnet-4-6, claude-opus-4-6\n"
        result = runner.invoke(main, ["init", str(tmp_path)], input=input_text)
        assert "codeprobe mine" in result.output
        assert "codeprobe run" in result.output
        assert "codeprobe interpret" in result.output

    def test_experiment_json_created(self, tmp_path: Path) -> None:
        runner = CliRunner()
        input_text = "2\nmy-test\nclaude\nclaude-sonnet-4-6, claude-opus-4-6\n"
        result = runner.invoke(main, ["init", str(tmp_path)], input=input_text)
        assert result.exit_code == 0, result.output
        exp_json = tmp_path / ".codeprobe" / "my-test" / "experiment.json"
        assert exp_json.exists()
        data = json.loads(exp_json.read_text())
        assert data["name"] == "my-test"
        assert len(data["configs"]) == 2
