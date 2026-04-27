"""Regression tests for MCP tool-surface policy resolution.

Covers the bead ``codeprobe-p6vw`` acceptance criteria:

* a) When ``mcp_config`` is set without explicit ``allowed_tools``, the
  executor auto-applies the strict restriction.
* b) Explicit ``allowed_tools`` (or ``disallowed_tools``) on the config
  wins — no auto-injection happens.
* c) ``mcp_mode='loose'`` preserves dual-surface behavior with a
  warning string.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from codeprobe.adapters.protocol import AgentConfig
from codeprobe.core.mcp_policy import (
    DEFAULT_MCP_MODE,
    MCPToolPolicy,
    resolve_tool_policy,
)
from codeprobe.models.experiment import ExperimentConfig

_SG_CONFIG = {
    "mcpServers": {
        "sourcegraph": {
            "type": "http",
            "url": "https://sourcegraph.com/.api/mcp/v1",
            "headers": {"Authorization": "token xyz"},
        }
    }
}

_MULTI_SERVER_CONFIG = {
    "mcpServers": {
        "sourcegraph": {"type": "http", "url": "https://x"},
        "github": {"type": "http", "url": "https://y"},
    }
}


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_default_mcp_mode_is_strict(self) -> None:
        assert DEFAULT_MCP_MODE == "strict"

    def test_experiment_config_defaults_to_strict(self) -> None:
        cfg = ExperimentConfig(label="x")
        assert cfg.mcp_mode == "strict"


# ---------------------------------------------------------------------------
# No mcp_config → policy is a passthrough
# ---------------------------------------------------------------------------


class TestNoMCPConfig:
    def test_returns_user_values_when_mcp_config_absent(self) -> None:
        cfg = ExperimentConfig(
            label="baseline",
            allowed_tools=["Read"],
            disallowed_tools=["Bash"],
        )
        policy = resolve_tool_policy(cfg)
        assert policy.allowed_tools == ["Read"]
        assert policy.disallowed_tools == ["Bash"]
        assert policy.warning is None

    def test_returns_none_when_user_set_nothing(self) -> None:
        cfg = ExperimentConfig(label="baseline")
        policy = resolve_tool_policy(cfg)
        assert policy.allowed_tools is None
        assert policy.disallowed_tools is None
        assert policy.warning is None


# ---------------------------------------------------------------------------
# Acceptance (a) — auto-restrict on mcp_config without explicit allow/disallow
# ---------------------------------------------------------------------------


class TestStrictAutoRestrict:
    def test_strict_default_blocks_grep_bash_glob_read(self) -> None:
        cfg = ExperimentConfig(label="with-mcp", mcp_config=_SG_CONFIG)
        policy = resolve_tool_policy(cfg)
        assert policy.mode == "strict"
        assert policy.warning is None
        assert policy.disallowed_tools == ["Grep", "Bash", "Glob", "Read"]

    def test_strict_default_allows_only_mcp_servers_and_write(self) -> None:
        cfg = ExperimentConfig(label="with-mcp", mcp_config=_SG_CONFIG)
        policy = resolve_tool_policy(cfg)
        assert policy.allowed_tools == ["mcp__sourcegraph", "Write"]

    def test_strict_handles_multi_server_mcp_config(self) -> None:
        cfg = ExperimentConfig(
            label="with-mcp",
            mcp_config=_MULTI_SERVER_CONFIG,
        )
        policy = resolve_tool_policy(cfg)
        assert "mcp__sourcegraph" in policy.allowed_tools
        assert "mcp__github" in policy.allowed_tools
        assert "Write" in policy.allowed_tools
        # Read still blocked under strict.
        assert "Read" in policy.disallowed_tools

    def test_pragmatic_allows_read(self) -> None:
        cfg = ExperimentConfig(
            label="with-mcp",
            mcp_config=_SG_CONFIG,
            mcp_mode="pragmatic",
        )
        policy = resolve_tool_policy(cfg)
        assert policy.mode == "pragmatic"
        assert "Read" in policy.allowed_tools
        assert "Write" in policy.allowed_tools
        assert "mcp__sourcegraph" in policy.allowed_tools
        assert policy.disallowed_tools == ["Grep", "Bash", "Glob"]
        assert "Read" not in policy.disallowed_tools


# ---------------------------------------------------------------------------
# Acceptance (b) — explicit allowed_tools wins (user override)
# ---------------------------------------------------------------------------


class TestUserOverride:
    def test_explicit_allowed_tools_wins(self) -> None:
        cfg = ExperimentConfig(
            label="with-mcp",
            mcp_config=_SG_CONFIG,
            allowed_tools=["Bash", "Read"],
        )
        policy = resolve_tool_policy(cfg)
        assert policy.mode == "explicit"
        assert policy.allowed_tools == ["Bash", "Read"]
        # No auto-blocklist either — user is in charge of the surface.
        assert policy.disallowed_tools is None

    def test_explicit_disallowed_tools_wins_too(self) -> None:
        cfg = ExperimentConfig(
            label="with-mcp",
            mcp_config=_SG_CONFIG,
            disallowed_tools=["Bash"],
        )
        policy = resolve_tool_policy(cfg)
        assert policy.mode == "explicit"
        assert policy.disallowed_tools == ["Bash"]
        assert policy.allowed_tools is None

    def test_empty_allowed_tools_is_treated_as_explicit(self) -> None:
        # ``allowed_tools=[]`` is the legacy "MCP-only, no built-ins"
        # opt-in. We must respect it as an explicit user choice and not
        # silently widen it back open.
        cfg = ExperimentConfig(
            label="with-mcp",
            mcp_config=_SG_CONFIG,
            allowed_tools=[],
        )
        policy = resolve_tool_policy(cfg)
        assert policy.mode == "explicit"
        assert policy.allowed_tools == []


# ---------------------------------------------------------------------------
# Acceptance (c) — mcp_mode='loose' preserves dual-surface with warning
# ---------------------------------------------------------------------------


class TestLooseMode:
    def test_loose_returns_no_restrictions(self) -> None:
        cfg = ExperimentConfig(
            label="with-mcp",
            mcp_config=_SG_CONFIG,
            mcp_mode="loose",
        )
        policy = resolve_tool_policy(cfg)
        assert policy.mode == "loose"
        assert policy.allowed_tools is None
        assert policy.disallowed_tools is None

    def test_loose_emits_warning(self) -> None:
        cfg = ExperimentConfig(
            label="with-mcp",
            mcp_config=_SG_CONFIG,
            mcp_mode="loose",
        )
        policy = resolve_tool_policy(cfg)
        assert policy.warning is not None
        assert "loose" in policy.warning
        assert "validity" in policy.warning


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_unknown_mode_raises(self) -> None:
        cfg = ExperimentConfig(
            label="with-mcp",
            mcp_config=_SG_CONFIG,
            mcp_mode="bogus",
        )
        with pytest.raises(ValueError, match="Invalid mcp_mode"):
            resolve_tool_policy(cfg)

    def test_policy_dataclass_is_frozen(self) -> None:
        policy = MCPToolPolicy(
            allowed_tools=None,
            disallowed_tools=None,
            mode="strict",
        )
        with pytest.raises((AttributeError, TypeError)):
            policy.mode = "pragmatic"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Wiring: api.py + run_cmd.py both apply the policy
# ---------------------------------------------------------------------------


class TestExperimentJsonRoundTrip:
    """experiment.json must persist mcp_mode across save/load."""

    def test_load_defaults_legacy_configs_to_strict(self, tmp_path: Path) -> None:
        from codeprobe.core.experiment import load_experiment

        exp_dir = tmp_path / "exp"
        exp_dir.mkdir()
        (exp_dir / "experiment.json").write_text(
            '{"name": "x", "configs": ['
            '{"label": "with-mcp", "mcp_config": {"mcpServers": {"sg": {}}}}'
            "]}",
            encoding="utf-8",
        )
        exp = load_experiment(exp_dir)
        assert exp.configs[0].mcp_mode == "strict"

    def test_load_respects_explicit_mcp_mode(self, tmp_path: Path) -> None:
        from codeprobe.core.experiment import load_experiment

        exp_dir = tmp_path / "exp"
        exp_dir.mkdir()
        (exp_dir / "experiment.json").write_text(
            '{"name": "x", "configs": ['
            '{"label": "with-mcp", "mcp_mode": "loose", '
            '"mcp_config": {"mcpServers": {"sg": {}}}}'
            "]}",
            encoding="utf-8",
        )
        exp = load_experiment(exp_dir)
        assert exp.configs[0].mcp_mode == "loose"

    def test_save_serializes_mcp_mode(self, tmp_path: Path) -> None:
        import json

        from codeprobe.core.experiment import save_experiment
        from codeprobe.models.experiment import Experiment

        exp = Experiment(
            name="x",
            configs=[
                ExperimentConfig(
                    label="with-mcp",
                    mcp_config=_SG_CONFIG,
                    mcp_mode="pragmatic",
                ),
            ],
        )
        exp_dir = tmp_path / "exp"
        exp_dir.mkdir()
        save_experiment(exp_dir, exp)
        data = json.loads((exp_dir / "experiment.json").read_text())
        assert data["configs"][0]["mcp_mode"] == "pragmatic"


class TestApiAppliesPolicy:
    """``run_experiment`` builds AgentConfig with policy-resolved tools."""

    def _setup_experiment(self, tmp_path: Path, *, mcp_mode: str) -> Path:
        import json

        exp_dir = tmp_path / "exp"
        (exp_dir / "tasks" / "t-1").mkdir(parents=True)
        (exp_dir / "tasks" / "t-1" / "instruction.md").write_text(
            "do something", encoding="utf-8"
        )
        config_entry: dict = {
            "label": "with-mcp",
            "agent": "claude",
            "mcp_config": _SG_CONFIG,
            "mcp_mode": mcp_mode,
        }
        (exp_dir / "experiment.json").write_text(
            json.dumps({"name": "x", "configs": [config_entry]}),
            encoding="utf-8",
        )
        return exp_dir

    def test_api_strict_mode_passes_restricted_tools(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from codeprobe import api as api_mod

        captured: dict[str, AgentConfig] = {}

        def _fake_execute(
            adapter, task_dirs, repo_path, experiment_config, agent_config, **kwargs
        ):  # noqa: ANN001 — test stub
            captured["agent_config"] = agent_config
            return []

        class _FakeAdapter:
            name = "claude"

            def preflight(self, _config: AgentConfig) -> list[str]:
                return []

        def _fake_resolve(name: str):  # noqa: ANN001 — test stub
            return _FakeAdapter()

        monkeypatch.setattr(api_mod, "execute_config", _fake_execute)
        monkeypatch.setattr(api_mod, "resolve", _fake_resolve)

        exp_dir = self._setup_experiment(tmp_path, mcp_mode="strict")
        api_mod.run_experiment(exp_dir)

        agent_config = captured["agent_config"]
        assert agent_config.allowed_tools == ["mcp__sourcegraph", "Write"]
        assert agent_config.disallowed_tools == ["Grep", "Bash", "Glob", "Read"]

    def test_api_loose_mode_logs_warning_and_no_restriction(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from codeprobe import api as api_mod

        captured: dict[str, AgentConfig] = {}

        def _fake_execute(
            adapter, task_dirs, repo_path, experiment_config, agent_config, **kwargs
        ):  # noqa: ANN001 — test stub
            captured["agent_config"] = agent_config
            return []

        class _FakeAdapter:
            name = "claude"

            def preflight(self, _config: AgentConfig) -> list[str]:
                return []

        def _fake_resolve(name: str):  # noqa: ANN001 — test stub
            return _FakeAdapter()

        monkeypatch.setattr(api_mod, "execute_config", _fake_execute)
        monkeypatch.setattr(api_mod, "resolve", _fake_resolve)

        exp_dir = self._setup_experiment(tmp_path, mcp_mode="loose")
        with caplog.at_level(logging.WARNING, logger="codeprobe.api"):
            api_mod.run_experiment(exp_dir)

        agent_config = captured["agent_config"]
        assert agent_config.allowed_tools is None
        assert agent_config.disallowed_tools is None
        assert any("loose" in rec.getMessage() for rec in caplog.records)
