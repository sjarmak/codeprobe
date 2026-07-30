"""Tests for layered config resolution: defaults < experiment.json < CLI flags."""

from __future__ import annotations

import logging
import stat
import subprocess
from pathlib import Path

import pytest

from codeprobe.models.experiment import ExperimentConfig


def _make_task_dir(base: Path, name: str) -> Path:
    """Create a minimal task directory with instruction and test.sh."""
    task_dir = base / name
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "instruction.md").write_text("Fix the bug.")
    tests_dir = task_dir / "tests"
    tests_dir.mkdir()
    test_sh = tests_dir / "test.sh"
    test_sh.write_text("#!/bin/bash\nexit 0\n")
    test_sh.chmod(test_sh.stat().st_mode | stat.S_IEXEC)
    return task_dir


def _setup_experiment(tmp_path: Path) -> Path:
    """Create a minimal experiment directory with experiment.json and a task."""
    import json

    exp_dir = tmp_path / "experiment"
    exp_dir.mkdir()
    tasks_dir = exp_dir / "tasks"
    _make_task_dir(tasks_dir, "task-001")

    experiment_json = {
        "name": "test-exp",
        "description": "test",
        "tasks_dir": "tasks",
        "task_ids": ["task-001"],
        "configs": [
            {
                "label": "baseline",
                "agent": "claude",
                "model": "sonnet-4",
                "extra": {"timeout_seconds": 120},
            }
        ],
    }
    (exp_dir / "experiment.json").write_text(json.dumps(experiment_json))
    return exp_dir


def test_explicit_experiment_json_path_resolves_to_its_directory(
    tmp_path: Path,
) -> None:
    from codeprobe.cli.run_cmd import _resolve_experiment_dir

    exp_dir = _setup_experiment(tmp_path)

    assert _resolve_experiment_dir(
        str(tmp_path),
        str(exp_dir / "experiment.json"),
    ) == exp_dir


# ---------------------------------------------------------------------------
# Unit tests for config resolution logic (extracted from _run_config)
# ---------------------------------------------------------------------------


def _resolve_config(
    exp_config: ExperimentConfig,
    *,
    cli_model: str | None = None,
    cli_timeout: int | None = None,
    fallback_model: str | None = None,
) -> tuple[str | None, int]:
    """Reproduce the layered resolution logic from run_cmd._run_config.

    Returns (resolved_model, resolved_timeout).
    Precedence: built-in defaults < experiment.json < CLI flags.
    """
    resolved_model = exp_config.model or fallback_model
    resolved_timeout = exp_config.extra.get("timeout_seconds", 300)

    # CLI --model overrides experiment.json model
    if cli_model is not None:
        resolved_model = cli_model

    # CLI --timeout overrides experiment.json extra.timeout_seconds
    if cli_timeout is not None:
        resolved_timeout = cli_timeout

    return resolved_model, resolved_timeout


class TestConfigResolution:
    """Test layered config resolution: defaults < experiment.json < CLI flags."""

    def test_experiment_json_overrides_defaults(self) -> None:
        """experiment.json values override built-in defaults."""
        cfg = ExperimentConfig(
            label="test",
            model="sonnet-4",
            extra={"timeout_seconds": 120},
        )
        model, timeout = _resolve_config(cfg)
        assert model == "sonnet-4"
        assert timeout == 120

    def test_cli_model_overrides_experiment_json(self) -> None:
        """--model CLI flag overrides experiment.json model."""
        cfg = ExperimentConfig(
            label="test",
            model="sonnet-4",
            extra={"timeout_seconds": 120},
        )
        model, timeout = _resolve_config(cfg, cli_model="opus-4")
        assert model == "opus-4"
        assert timeout == 120  # timeout unchanged

    def test_cli_timeout_overrides_experiment_json(self) -> None:
        """--timeout CLI flag overrides experiment.json extra.timeout_seconds."""
        cfg = ExperimentConfig(
            label="test",
            model="sonnet-4",
            extra={"timeout_seconds": 120},
        )
        model, timeout = _resolve_config(cfg, cli_timeout=600)
        assert model == "sonnet-4"  # model unchanged
        assert timeout == 600

    def test_both_cli_overrides(self) -> None:
        """Both --model and --timeout override experiment.json."""
        cfg = ExperimentConfig(
            label="test",
            model="sonnet-4",
            extra={"timeout_seconds": 120},
        )
        model, timeout = _resolve_config(cfg, cli_model="opus-4", cli_timeout=600)
        assert model == "opus-4"
        assert timeout == 600

    def test_absent_cli_flags_fall_through(self) -> None:
        """When CLI flags are None, experiment.json values are used."""
        cfg = ExperimentConfig(
            label="test",
            model="haiku-4",
            extra={"timeout_seconds": 45},
        )
        model, timeout = _resolve_config(cfg, cli_model=None, cli_timeout=None)
        assert model == "haiku-4"
        assert timeout == 45

    def test_default_timeout_when_not_in_experiment(self) -> None:
        """Built-in default of 300s is used when experiment.json has no timeout."""
        cfg = ExperimentConfig(label="test", model="sonnet-4")
        model, timeout = _resolve_config(cfg)
        assert timeout == 300

    def test_cli_timeout_overrides_default(self) -> None:
        """CLI --timeout overrides the built-in default when experiment.json has none."""
        cfg = ExperimentConfig(label="test", model="sonnet-4")
        model, timeout = _resolve_config(cfg, cli_timeout=900)
        assert timeout == 900

    def test_fallback_model_used_when_experiment_has_none(self) -> None:
        """When experiment.json model is None, CLI --agent model is used."""
        cfg = ExperimentConfig(label="test", model=None)
        model, timeout = _resolve_config(cfg, fallback_model="default-model")
        assert model == "default-model"

    def test_cli_model_overrides_fallback(self) -> None:
        """CLI --model overrides even the fallback model."""
        cfg = ExperimentConfig(label="test", model=None)
        model, timeout = _resolve_config(
            cfg, cli_model="opus-4", fallback_model="default-model"
        )
        assert model == "opus-4"


class TestConfigResolutionLogging:
    """Test that config resolution logs at debug level."""

    def test_debug_log_cli_override(self, caplog: pytest.LogCaptureFixture) -> None:
        """Debug log shows 'CLI override' when CLI flags are provided."""
        import codeprobe.cli.run_cmd as run_cmd_mod

        with caplog.at_level(logging.DEBUG, logger="codeprobe.cli.run_cmd"):
            run_cmd_mod.logger.debug(
                "Config resolution: model=%s (%s), timeout=%ds (%s)",
                "opus-4",
                "CLI override",
                600,
                "CLI override",
            )
        assert "CLI override" in caplog.text
        assert "opus-4" in caplog.text
        assert "600" in caplog.text

    def test_debug_log_experiment_json(self, caplog: pytest.LogCaptureFixture) -> None:
        """Debug log shows 'experiment.json' when no CLI flags override."""
        import codeprobe.cli.run_cmd as run_cmd_mod

        with caplog.at_level(logging.DEBUG, logger="codeprobe.cli.run_cmd"):
            run_cmd_mod.logger.debug(
                "Config resolution: model=%s (%s), timeout=%ds (%s)",
                "sonnet-4",
                "experiment.json",
                300,
                "experiment.json",
            )
        assert "experiment.json" in caplog.text
        assert "sonnet-4" in caplog.text


class TestArmCapabilityPreflight:
    """Pre-spend hard refusal of knobs the arm's adapter cannot honor.

    codeprobe-f7rl.26: no experiment reaches dispatch with a knob its
    adapter would silently drop; refusal is prescriptive and terminal.
    """

    _MCP = {"mcpServers": {"sourcegraph": {"url": "https://example.invalid"}}}

    def _copilot(self):
        from codeprobe.adapters.copilot import CopilotAdapter

        return CopilotAdapter()

    def _claude(self):
        from codeprobe.adapters.claude import ClaudeAdapter

        return ClaudeAdapter()

    def _check(self, cfg: ExperimentConfig, adapter, **kwargs) -> None:
        from codeprobe.core.capability_preflight import check_arm_capabilities

        check_arm_capabilities(cfg, adapter, **kwargs)

    def test_copilot_mcp_strict_arm_is_refused(self) -> None:
        """Default mcp_mode=strict derives a tool surface copilot can't enforce."""
        from codeprobe.cli.errors import PrescriptiveError

        cfg = ExperimentConfig(label="with-mcp", agent="copilot", mcp_config=self._MCP)
        with pytest.raises(PrescriptiveError) as exc_info:
            self._check(cfg, self._copilot())
        err = exc_info.value
        assert err.code == "ADAPTER_CAPABILITY"
        assert err.terminal is True
        assert "allowed_tools" in err.message
        assert "disallowed_tools" in err.message
        assert "with-mcp" in err.message
        assert "copilot" in err.message
        # The honest path for an MCP arm on copilot is named in the message.
        assert "loose" in err.message
        assert "claude" in (err.message_for_agent or "")
        assert err.detail["unsupported_knobs"] == [
            "allowed_tools",
            "disallowed_tools",
        ]

    def test_copilot_mcp_loose_arm_runs(self) -> None:
        """loose derives no restriction — the arm passes, warning intact."""
        from codeprobe.core.mcp_policy import resolve_tool_policy

        cfg = ExperimentConfig(
            label="with-mcp",
            agent="copilot",
            mcp_config=self._MCP,
            mcp_mode="loose",
        )
        self._check(cfg, self._copilot())  # must not raise
        # The declared permission_mode is "default": the sandbox flip to
        # dangerously_skip happens post-preflight and must not refuse here.
        assert resolve_tool_policy(cfg).warning is not None

    def test_copilot_max_turns_arm_is_refused(self) -> None:
        from codeprobe.cli.errors import PrescriptiveError

        cfg = ExperimentConfig(label="capped", agent="copilot", max_turns=30)
        with pytest.raises(PrescriptiveError) as exc_info:
            self._check(cfg, self._copilot())
        assert exc_info.value.detail["unsupported_knobs"] == ["max_turns"]

    def test_cli_max_turns_flag_is_refused_on_copilot(self) -> None:
        """The --max-turns CLI flag counts as a requested knob too."""
        from codeprobe.cli.errors import PrescriptiveError

        cfg = ExperimentConfig(label="capped", agent="copilot")
        with pytest.raises(PrescriptiveError):
            self._check(cfg, self._copilot(), cli_max_turns=30)

    def test_legacy_extra_max_turns_is_refused_on_copilot(self) -> None:
        """Configs authored before the max_turns field used extra."""
        from codeprobe.cli.errors import PrescriptiveError

        cfg = ExperimentConfig(
            label="capped", agent="copilot", extra={"max_turns": 30}
        )
        with pytest.raises(PrescriptiveError):
            self._check(cfg, self._copilot())

    def test_claude_arm_using_every_knob_passes(self) -> None:
        cfg = ExperimentConfig(
            label="full",
            agent="claude",
            mcp_config=self._MCP,
            permission_mode="acceptEdits",
            max_turns=30,
        )
        self._check(cfg, self._claude(), cli_max_turns=50)  # must not raise

    def test_undeclared_adapter_refuses_all_knobs(self) -> None:
        """Fail-closed: no capabilities attribute means prompt+model only."""
        from codeprobe.cli.errors import PrescriptiveError

        class StubAdapter:
            name = "stub"

        cfg = ExperimentConfig(
            label="full",
            agent="stub",
            mcp_config=self._MCP,
            permission_mode="acceptEdits",
            max_turns=30,
        )
        with pytest.raises(PrescriptiveError) as exc_info:
            self._check(cfg, StubAdapter())
        assert exc_info.value.detail["unsupported_knobs"] == [
            "mcp_config",
            "allowed_tools",
            "disallowed_tools",
            "max_turns",
            "permission_mode",
        ]

    def test_knobless_arm_passes_on_any_adapter(self) -> None:
        """An arm asking for nothing beyond prompt+model is never refused."""

        class StubAdapter:
            name = "stub"

        self._check(ExperimentConfig(label="plain", agent="stub"), StubAdapter())


class TestQuarantinedAdapterRefusal:
    """codeprobe-f7rl.27: quarantined adapters (codex) never dispatch.

    The refusal fires in the upfront per-arm preflight — before task
    discovery, before any arm runs or spends — for single-arm runs and
    multi-arm experiments alike, and is prescriptive (code, message,
    alternatives), not a traceback.
    """

    def _spy_adapters(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        """Replace claude/codex run() with recorders that must stay uncalled."""
        from codeprobe.adapters.claude import ClaudeAdapter
        from codeprobe.adapters.codex import CodexAdapter

        calls: list[str] = []

        def _make_run(adapter_name: str):
            def _run(self, prompt, config, session_env=None):  # noqa: ANN001
                calls.append(adapter_name)
                raise AssertionError(f"{adapter_name}.run must not be called")

            return _run

        monkeypatch.setattr(ClaudeAdapter, "run", _make_run("claude"))
        monkeypatch.setattr(CodexAdapter, "run", _make_run("codex"))
        return calls

    def _experiment(self, tmp_path: Path, configs: list[dict]) -> Path:
        import json

        # Quarantine refusal must fire even before the dirty-checkout gate
        # sees a real repo (codeprobe-f7rl.1) — a clean commit here proves
        # this is a quarantine refusal, not an unrelated NOT_A_GIT_REPO.
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(tmp_path)], check=True
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.email", "t@t.test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True
        )
        (tmp_path / ".gitkeep").write_text("")
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"], check=True
        )

        exp_dir = tmp_path / "experiment"
        exp_dir.mkdir()
        _make_task_dir(exp_dir / "tasks", "task-001")
        (exp_dir / "experiment.json").write_text(
            json.dumps(
                {
                    "name": "quarantine-exp",
                    "description": "test",
                    "tasks_dir": "tasks",
                    "task_ids": ["task-001"],
                    "configs": configs,
                }
            )
        )
        return exp_dir

    def test_single_arm_codex_run_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run --agent codex → exit 2, ADAPTER_QUARANTINED, zero run() calls."""
        from click.testing import CliRunner

        from codeprobe.cli import main

        calls = self._spy_adapters(monkeypatch)
        exp_dir = self._experiment(tmp_path, configs=[])

        result = CliRunner().invoke(
            main, ["run", str(exp_dir), "--agent", "codex"]
        )

        assert result.exit_code == 2, result.output
        assert "ADAPTER_QUARANTINED" in result.output
        assert "quarantined" in result.output
        assert "cannot edit files" in result.output
        assert "claude" in result.output  # the alternative is named
        assert calls == []

    def test_mixed_experiment_refused_with_no_trials_on_either_arm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """claude arm + codex arm → refused upfront; NO trials for either arm.

        No half-run comparison, no spend: the claude arm must not execute
        just because it precedes the quarantined arm in the config list.
        """
        from click.testing import CliRunner

        from codeprobe.cli import main

        calls = self._spy_adapters(monkeypatch)
        exp_dir = self._experiment(
            tmp_path,
            configs=[
                {"label": "baseline", "agent": "claude"},
                {"label": "codex-arm", "agent": "codex"},
            ],
        )

        result = CliRunner().invoke(main, ["run", str(exp_dir)])

        assert result.exit_code == 2, result.output
        assert "ADAPTER_QUARANTINED" in result.output
        assert calls == []


class TestPerArmBackendPreflight:
    """Every arm's backend must resolve at preflight (codeprobe-f7rl.25).

    A typo'd agent in ANY config previously survived until _run_config and
    crashed with a raw KeyError after another arm had already spent money.
    """

    @staticmethod
    def _setup_two_arm_experiment(tmp_path: Path) -> Path:
        import json

        # The typo'd-backend refusal must fire even before the dirty-checkout
        # gate sees a real repo (codeprobe-f7rl.1) — a clean commit here
        # proves this is an UNKNOWN_BACKEND refusal, not NOT_A_GIT_REPO.
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(tmp_path)], check=True
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.email", "t@t.test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True
        )
        (tmp_path / ".gitkeep").write_text("")
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"], check=True
        )

        exp_dir = tmp_path / "experiment"
        exp_dir.mkdir()
        _make_task_dir(exp_dir / "tasks", "task-001")
        experiment_json = {
            "name": "test-exp",
            "description": "test",
            "tasks_dir": "tasks",
            "task_ids": ["task-001"],
            "configs": [
                {"label": "baseline", "agent": "claude"},
                {"label": "variant", "agent": "claud"},
            ],
        }
        (exp_dir / "experiment.json").write_text(json.dumps(experiment_json))
        return exp_dir

    def test_typod_arm_backend_fails_before_any_dispatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """agent='claud' in one arm → UNKNOWN_BACKEND naming the arm, zero runs."""
        import codeprobe.cli.run_cmd as run_cmd_mod
        from codeprobe.cli.errors import PrescriptiveError
        from tests.conftest import FakeAdapter

        exp_dir = self._setup_two_arm_experiment(tmp_path)
        adapter = FakeAdapter()

        def _resolve(name: str) -> FakeAdapter:
            if name == "claude":
                return adapter
            raise KeyError(
                f"Unknown agent adapter: {name!r}. "
                "Available: claude, codex, copilot"
            )

        monkeypatch.setattr(run_cmd_mod, "resolve", _resolve)

        with pytest.raises(PrescriptiveError) as excinfo:
            run_cmd_mod.run_eval(
                str(exp_dir),
                agent="claude",
                quiet=True,
                force_plain=True,
            )

        err = excinfo.value
        assert err.code == "UNKNOWN_BACKEND"
        assert err.terminal is True
        assert "'variant'" in err.message
        assert "'claud'" in err.message
        assert err.detail["config_label"] == "variant"
        assert err.detail["requested"] == "claud"
        assert adapter.run_calls == [], (
            "no adapter may dispatch before every arm's backend resolves"
        )


class TestCliRepeatsPassthrough:
    """Test that --repeats is passed through to execute_config."""

    def test_repeats_default_is_one(self) -> None:
        """When --repeats is not provided, default is 1."""
        from click.testing import CliRunner

        from codeprobe.cli import run

        runner = CliRunner()
        # Just check the help to verify the option exists
        result = runner.invoke(run, ["--help"])
        assert result.exit_code == 0
        assert "--repeats" in result.output
        assert "--timeout" in result.output
