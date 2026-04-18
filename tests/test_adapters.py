"""Tests for agent adapter protocol and implementations."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codeprobe.adapters._base import BaseAdapter
from codeprobe.adapters.claude import ClaudeAdapter
from codeprobe.adapters.copilot import CopilotAdapter
from codeprobe.adapters.protocol import (
    ALLOWED_COST_MODELS,
    ALLOWED_COST_SOURCES,
    AdapterError,
    AdapterExecutionError,
    AdapterSetupError,
    AgentAdapter,
    AgentConfig,
    AgentOutput,
)


def test_claude_adapter_is_agent_adapter():
    adapter = ClaudeAdapter()
    assert isinstance(adapter, AgentAdapter)
    assert adapter.name == "claude"


def test_copilot_adapter_is_agent_adapter():
    adapter = CopilotAdapter()
    assert isinstance(adapter, AgentAdapter)
    assert adapter.name == "copilot"


def test_claude_build_command():
    adapter = ClaudeAdapter()
    config = AgentConfig(model="claude-sonnet-4-6")
    # Only test if claude binary exists
    if adapter.find_binary():
        cmd = adapter.build_command("fix the bug", config)
        assert "-p" in cmd
        assert "fix the bug" in cmd
        assert "--model" in cmd


def test_copilot_preflight_no_mcp_warning():
    adapter = CopilotAdapter()
    config = AgentConfig(mcp_config={"tools": ["search"]})
    issues = adapter.preflight(config)
    mcp_warnings = [i for i in issues if "MCP" in i]
    assert len(mcp_warnings) == 0


def test_agent_output_is_frozen():
    output = AgentOutput(
        stdout="result",
        stderr=None,
        exit_code=0,
        duration_seconds=1.5,
    )
    assert output.stdout == "result"
    assert output.cost_usd is None


def test_agent_config_defaults():
    config = AgentConfig()
    assert config.permission_mode == "default"
    assert config.timeout_seconds == 3600


def test_claude_permission_mode_passed():
    adapter = ClaudeAdapter()
    config = AgentConfig(permission_mode="plan")
    if adapter.find_binary():
        cmd = adapter.build_command("test", config)
        assert "--permission-mode" in cmd
        idx = cmd.index("--permission-mode")
        assert cmd[idx + 1] == "plan"


def test_claude_default_permission_mode_omitted():
    adapter = ClaudeAdapter()
    config = AgentConfig(permission_mode="default")
    if adapter.find_binary():
        cmd = adapter.build_command("test", config)
        assert "--permission-mode" not in cmd


def test_claude_rejects_bypass_permissions():
    adapter = ClaudeAdapter()
    config = AgentConfig(permission_mode="bypassPermissions")
    if adapter.find_binary():
        with pytest.raises(ValueError, match="Unsafe permission_mode"):
            adapter.build_command("test", config)


# -- AgentOutput token/cost fields -------------------------------------------


class TestAgentOutputTokenFields:
    def test_default_token_fields_are_none(self) -> None:
        output = AgentOutput(stdout="ok", stderr=None, exit_code=0, duration_seconds=1.0)
        assert output.input_tokens is None
        assert output.output_tokens is None
        assert output.cache_read_tokens is None
        assert output.cost_model == "unknown"
        assert output.tool_call_count is None

    def test_tool_call_count(self) -> None:
        output = AgentOutput(
            stdout="ok",
            stderr=None,
            exit_code=0,
            duration_seconds=1.0,
            tool_call_count=5,
        )
        assert output.tool_call_count == 5

    def test_per_token_cost_model(self) -> None:
        output = AgentOutput(
            stdout="ok",
            stderr=None,
            exit_code=0,
            duration_seconds=1.0,
            input_tokens=1000,
            output_tokens=500,
            cache_read_tokens=200,
            cost_model="per_token",
            cost_usd=0.05,
        )
        assert output.input_tokens == 1000
        assert output.output_tokens == 500
        assert output.cache_read_tokens == 200
        assert output.cost_model == "per_token"
        assert output.cost_usd == 0.05

    def test_subscription_cost_model(self) -> None:
        output = AgentOutput(
            stdout="ok",
            stderr=None,
            exit_code=0,
            duration_seconds=1.0,
            cost_model="subscription",
        )
        assert output.cost_model == "subscription"
        assert output.cost_usd is None

    def test_allowed_cost_models(self) -> None:
        assert "per_token" in ALLOWED_COST_MODELS
        assert "subscription" in ALLOWED_COST_MODELS
        assert "unknown" in ALLOWED_COST_MODELS


class TestAgentOutputValidation:
    def test_invalid_cost_model_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown cost_model"):
            AgentOutput(
                stdout="ok",
                stderr=None,
                exit_code=0,
                duration_seconds=1.0,
                cost_model="magic",
            )

    def test_per_token_without_cost_usd_raises(self) -> None:
        with pytest.raises(ValueError, match="cost_usd is required"):
            AgentOutput(
                stdout="ok",
                stderr=None,
                exit_code=0,
                duration_seconds=1.0,
                cost_model="per_token",
            )


# -- Narrowed Protocol --------------------------------------------------------


class TestNarrowedProtocol:
    def test_minimal_adapter_satisfies_protocol(self) -> None:
        """A class with only name/preflight/run satisfies AgentAdapter."""

        class MinimalAdapter:
            @property
            def name(self) -> str:
                return "minimal"

            def preflight(self, config: AgentConfig) -> list[str]:
                return []

            def run(self, prompt: str, config: AgentConfig) -> AgentOutput:
                return AgentOutput(stdout="ok", stderr=None, exit_code=0, duration_seconds=0.1)

            def isolate_session(self, slot_id: int) -> dict[str, str]:
                return {}

        adapter = MinimalAdapter()
        assert isinstance(adapter, AgentAdapter)
        assert adapter.name == "minimal"


# -- AgentOutput error / cost_source fields ------------------------------------


class TestAgentOutputErrorField:
    def test_default_is_none(self) -> None:
        output = AgentOutput(stdout="ok", stderr=None, exit_code=0, duration_seconds=1.0)
        assert output.error is None

    def test_can_set_error(self) -> None:
        output = AgentOutput(
            stdout="",
            stderr=None,
            exit_code=1,
            duration_seconds=1.0,
            error="Agent timed out after 300s",
        )
        assert output.error == "Agent timed out after 300s"


class TestAgentOutputCostSource:
    def test_default_is_unavailable(self) -> None:
        output = AgentOutput(stdout="ok", stderr=None, exit_code=0, duration_seconds=1.0)
        assert output.cost_source == "unavailable"

    @pytest.mark.parametrize("source", sorted(ALLOWED_COST_SOURCES))
    def test_valid_values_accepted(self, source: str) -> None:
        output = AgentOutput(
            stdout="ok",
            stderr=None,
            exit_code=0,
            duration_seconds=1.0,
            cost_source=source,
        )
        assert output.cost_source == source

    def test_invalid_cost_source_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown cost_source"):
            AgentOutput(
                stdout="ok",
                stderr=None,
                exit_code=0,
                duration_seconds=1.0,
                cost_source="magic",
            )


# -- AdapterError hierarchy ----------------------------------------------------


class TestAdapterErrorHierarchy:
    def test_all_subclasses_are_adapter_error(self) -> None:
        assert issubclass(AdapterSetupError, AdapterError)
        assert issubclass(AdapterExecutionError, AdapterError)

    def test_adapter_error_is_exception(self) -> None:
        assert issubclass(AdapterError, Exception)

    def test_can_catch_subclass_as_adapter_error(self) -> None:
        with pytest.raises(AdapterError):
            raise AdapterSetupError("binary missing")


# -- BaseAdapter run() error handling ------------------------------------------


class _StubAdapter(BaseAdapter):
    """Minimal concrete adapter for testing BaseAdapter.run()."""

    _binary_name = "fake-agent"
    _install_hint = "Install fake-agent"

    def build_command(self, prompt: str, config: AgentConfig) -> list[str]:
        return ["/usr/bin/fake-agent", "-p", prompt]


class TestBaseAdapterEnvWhitelist:
    """Verify subprocess.run() env handling: inherit when no isolation, filter when isolated."""

    def test_inherits_full_env_without_session_env(self) -> None:
        """Without session isolation, subprocess inherits the full parent env."""
        adapter = _StubAdapter()
        config = AgentConfig(timeout_seconds=5)
        fake_result = subprocess.CompletedProcess(args=["fake-agent"], returncode=0, stdout="ok", stderr="")
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            adapter.run("test", config)

        _, kwargs = mock_run.call_args
        assert kwargs.get("env") is None, "env=None inherits parent process env"

    def test_filters_env_with_session_env(self) -> None:
        """With session isolation, subprocess gets a filtered env."""
        adapter = _StubAdapter()
        config = AgentConfig(timeout_seconds=5)
        fake_result = subprocess.CompletedProcess(args=["fake-agent"], returncode=0, stdout="ok", stderr="")
        session_env = {"CLAUDE_CONFIG_DIR": "/tmp/test"}
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            adapter.run("test", config, session_env=session_env)

        env = mock_run.call_args[1]["env"]
        assert isinstance(env, dict)
        assert env.get("CLAUDE_CONFIG_DIR") == "/tmp/test"

    def test_filtered_env_includes_path_and_home(self) -> None:
        adapter = _StubAdapter()
        config = AgentConfig(timeout_seconds=5)
        fake_result = subprocess.CompletedProcess(args=["fake-agent"], returncode=0, stdout="ok", stderr="")
        session_env = {"CLAUDE_CONFIG_DIR": "/tmp/test"}
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            import os

            old_path = os.environ.get("PATH", "")
            old_home = os.environ.get("HOME", "")
            adapter.run("test", config, session_env=session_env)

        env = mock_run.call_args[1]["env"]
        if old_path:
            assert env.get("PATH") == old_path
        if old_home:
            assert env.get("HOME") == old_home

    def test_filtered_env_excludes_random_secrets(self) -> None:
        adapter = _StubAdapter()
        config = AgentConfig(timeout_seconds=5)
        fake_result = subprocess.CompletedProcess(args=["fake-agent"], returncode=0, stdout="ok", stderr="")
        session_env = {"CLAUDE_CONFIG_DIR": "/tmp/test"}
        import os

        os.environ["MY_SUPER_SECRET_DB_PASSWORD"] = "hunter2"
        try:
            with patch("subprocess.run", return_value=fake_result) as mock_run:
                adapter.run("test", config, session_env=session_env)
            env = mock_run.call_args[1]["env"]
            assert "MY_SUPER_SECRET_DB_PASSWORD" not in env
        finally:
            del os.environ["MY_SUPER_SECRET_DB_PASSWORD"]


class TestBaseAdapterRunErrors:
    def test_timeout_returns_error_output(self) -> None:
        adapter = _StubAdapter()
        config = AgentConfig(timeout_seconds=5)
        exc = subprocess.TimeoutExpired(cmd=["fake-agent"], timeout=5)
        exc.stdout = "partial out"
        exc.stderr = "partial err"
        with patch("subprocess.run", side_effect=exc):
            output = adapter.run("test prompt", config)
        assert "timed out" in output.error
        assert output.exit_code == -1
        assert output.stdout == "partial out"
        assert output.stderr == "partial err"

    def test_timeout_handles_none_output(self) -> None:
        adapter = _StubAdapter()
        config = AgentConfig(timeout_seconds=10)
        exc = subprocess.TimeoutExpired(cmd=["fake-agent"], timeout=10)
        exc.stdout = None
        exc.stderr = None
        with patch("subprocess.run", side_effect=exc):
            output = adapter.run("test", config)
        assert output.stdout == ""
        assert output.stderr is None
        assert output.error is not None

    def test_file_not_found_raises_setup_error(self) -> None:
        adapter = _StubAdapter()
        config = AgentConfig()
        with patch("subprocess.run", side_effect=FileNotFoundError("/usr/bin/fake-agent")):
            with pytest.raises(AdapterSetupError, match="Binary not found"):
                adapter.run("test", config)

    def test_require_binary_raises_setup_error(self) -> None:
        adapter = _StubAdapter()
        with patch.object(adapter, "find_binary", return_value=None):
            with pytest.raises(AdapterSetupError, match="CLI not found"):
                adapter._require_binary()


# -- Timeout telemetry extraction -----------------------------------------------


class TestTimeoutTelemetryExtraction:
    """When a process times out with partial stdout, parse_output() should
    be called to extract whatever telemetry is available."""

    def test_claude_timeout_extracts_partial_telemetry(self) -> None:
        """Claude adapter should extract tokens/cost from partial output on timeout."""
        adapter = ClaudeAdapter()
        config = AgentConfig(timeout_seconds=5)
        partial_json = json.dumps(
            {
                "result": "partial work...",
                "usage": {
                    "input_tokens": 8000,
                    "output_tokens": 2000,
                    "cache_read_input_tokens": 500,
                },
                "total_cost_usd": 0.035,
            }
        )
        exc = subprocess.TimeoutExpired(cmd=["claude"], timeout=5)
        exc.stdout = partial_json
        exc.stderr = ""
        with (
            patch("subprocess.run", side_effect=exc),
            patch.object(adapter, "find_binary", return_value="/usr/bin/claude"),
        ):
            output = adapter.run("test prompt", config)
        assert "timed out" in output.error
        assert output.exit_code == -1
        assert output.input_tokens == 8000
        assert output.output_tokens == 2000
        assert output.cache_read_tokens == 500
        assert output.cost_usd == pytest.approx(0.035)
        assert output.cost_source == "api_reported"

    def test_claude_timeout_with_invalid_json_still_returns_timeout_error(self) -> None:
        """When partial stdout is not valid JSON, timeout error is still reported."""
        adapter = ClaudeAdapter()
        config = AgentConfig(timeout_seconds=5)
        exc = subprocess.TimeoutExpired(cmd=["claude"], timeout=5)
        exc.stdout = "not valid json {{"
        exc.stderr = "some stderr"
        with (
            patch("subprocess.run", side_effect=exc),
            patch.object(adapter, "find_binary", return_value="/usr/bin/claude"),
        ):
            output = adapter.run("test prompt", config)
        assert "timed out" in output.error
        assert output.exit_code == -1
        assert output.input_tokens is None
        assert output.cost_usd is None

    def test_claude_timeout_with_none_stdout_returns_no_telemetry(self) -> None:
        """When timeout has no stdout at all, no telemetry is extracted."""
        adapter = ClaudeAdapter()
        config = AgentConfig(timeout_seconds=5)
        exc = subprocess.TimeoutExpired(cmd=["claude"], timeout=5)
        exc.stdout = None
        exc.stderr = None
        with (
            patch("subprocess.run", side_effect=exc),
            patch.object(adapter, "find_binary", return_value="/usr/bin/claude"),
        ):
            output = adapter.run("test prompt", config)
        assert "timed out" in output.error
        assert output.exit_code == -1
        assert output.input_tokens is None
        assert output.cost_usd is None

    def test_timeout_with_bytes_stdout_decoded(self) -> None:
        """TimeoutExpired.stdout can be bytes — should be decoded."""
        adapter = ClaudeAdapter()
        config = AgentConfig(timeout_seconds=5)
        partial_json = json.dumps(
            {
                "result": "partial",
                "usage": {"input_tokens": 100, "output_tokens": 50},
                "total_cost_usd": 0.001,
            }
        )
        exc = subprocess.TimeoutExpired(cmd=["claude"], timeout=5)
        exc.stdout = partial_json.encode("utf-8")
        exc.stderr = b"err"
        with (
            patch("subprocess.run", side_effect=exc),
            patch.object(adapter, "find_binary", return_value="/usr/bin/claude"),
        ):
            output = adapter.run("test prompt", config)
        assert "timed out" in output.error
        assert output.input_tokens == 100
        assert output.output_tokens == 50
        assert output.cost_usd == pytest.approx(0.001)

    def test_timeout_parse_output_failure_still_returns_timeout_error(self) -> None:
        """If parse_output itself raises on timeout path, we still get a valid AgentOutput."""

        class ExplodingParser(BaseAdapter):
            _binary_name = "exploding"
            _install_hint = "n/a"

            def build_command(self, prompt: str, config: AgentConfig) -> list[str]:
                return ["exploding", "-p", prompt]

            def parse_output(self, result: subprocess.CompletedProcess[str], duration: float) -> AgentOutput:
                raise RuntimeError("parser exploded")

        adapter = ExplodingParser()
        config = AgentConfig(timeout_seconds=5)
        exc = subprocess.TimeoutExpired(cmd=["exploding"], timeout=5)
        exc.stdout = "some output"
        exc.stderr = ""
        with patch("subprocess.run", side_effect=exc):
            output = adapter.run("test", config)
        assert "timed out" in output.error
        assert output.exit_code == -1


# -- BaseAdapter parse_output() ------------------------------------------------


class TestParseOutput:
    def test_default_maps_fields(self) -> None:
        adapter = _StubAdapter()
        result = subprocess.CompletedProcess(args=["fake-agent"], returncode=0, stdout="hello", stderr="")
        output = adapter.parse_output(result, duration=2.5)
        assert output.stdout == "hello"
        assert output.stderr is None  # empty string → None
        assert output.exit_code == 0
        assert output.duration_seconds == 2.5

    def test_parse_failure_returns_output_with_error(self) -> None:
        class BrokenParser(BaseAdapter):
            _binary_name = "broken"
            _install_hint = "n/a"

            def build_command(self, prompt: str, config: AgentConfig) -> list[str]:
                return ["broken"]

            def parse_output(self, result: subprocess.CompletedProcess[str], duration: float) -> AgentOutput:
                raise ValueError("bad JSON")

        adapter = BrokenParser()
        config = AgentConfig()
        fake_result = subprocess.CompletedProcess(args=["broken"], returncode=0, stdout="raw output", stderr="err")
        with patch("subprocess.run", return_value=fake_result):
            output = adapter.run("test", config)
        assert "Output parse failed" in output.error
        assert output.stdout == "raw output"


# -- ClaudeAdapter parse_output() ---------------------------------------------

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _make_result(fixture_name: str) -> subprocess.CompletedProcess[str]:
    """Load a fixture file and wrap it in a CompletedProcess."""
    content = (FIXTURE_DIR / fixture_name).read_text()
    return subprocess.CompletedProcess(args=["claude", "-p", "test"], returncode=0, stdout=content, stderr="")


class TestClaudeParseOutput:
    def test_normal(self) -> None:
        adapter = ClaudeAdapter()
        result = _make_result("claude_normal.json")
        output = adapter.parse_output(result, duration=1.0)

        assert output.stdout == "Here is the fix for the bug..."
        assert output.input_tokens == 12345
        assert output.output_tokens == 6789
        assert output.cache_read_tokens == 1000
        assert output.cost_usd == pytest.approx(0.0423)
        assert output.cost_model == "per_token"
        assert output.cost_source == "api_reported"
        assert output.error is None

    def test_no_usage(self) -> None:
        adapter = ClaudeAdapter()
        result = _make_result("claude_no_usage.json")
        output = adapter.parse_output(result, duration=1.0)

        assert output.input_tokens is None
        assert output.output_tokens is None
        assert output.cache_read_tokens is None
        assert output.cost_source == "unavailable"
        assert output.error is not None

    def test_partial_usage(self) -> None:
        adapter = ClaudeAdapter()
        result = _make_result("claude_partial_usage.json")
        output = adapter.parse_output(result, duration=1.0)

        assert output.input_tokens == 5000
        assert output.output_tokens is None
        assert output.cache_read_tokens is None
        assert output.cost_usd == pytest.approx(0.02)
        assert output.cost_model == "per_token"
        assert output.cost_source == "api_reported"

    def test_malformed_json(self) -> None:
        adapter = ClaudeAdapter()
        result = _make_result("claude_malformed.json")
        output = adapter.parse_output(result, duration=1.0)

        assert output.input_tokens is None
        assert output.output_tokens is None
        assert output.cost_source == "unavailable"
        assert output.error is not None
        assert "JSON" in output.error or "json" in output.error.lower()

    def test_empty_stdout(self) -> None:
        adapter = ClaudeAdapter()
        result = _make_result("claude_empty.json")
        output = adapter.parse_output(result, duration=1.0)

        assert output.input_tokens is None
        assert output.output_tokens is None
        assert output.cost_source == "unavailable"
        assert output.error is not None

    def test_tool_call_count_from_messages(self) -> None:
        adapter = ClaudeAdapter()
        result = _make_result("claude_with_tools.json")
        output = adapter.parse_output(result, duration=1.0)

        assert output.tool_call_count == 3
        assert output.error is None

    def test_tool_call_count_none_without_messages(self) -> None:
        adapter = ClaudeAdapter()
        result = _make_result("claude_normal.json")
        output = adapter.parse_output(result, duration=1.0)

        assert output.tool_call_count is None


# -- CopilotAdapter parse_output() --------------------------------------------


class TestCopilotParseOutput:
    """Tests for CopilotAdapter.parse_output() — requires structured NDJSON from CLI 1.0.4+."""

    @staticmethod
    def _load_fixture(name: str) -> str:
        return (FIXTURE_DIR / name).read_text()

    def _make_copilot_result(self, stdout: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["copilot"], returncode=0, stdout=stdout, stderr="")

    def test_ndjson_with_tokens(self) -> None:
        adapter = CopilotAdapter()
        # Mock log extraction to isolate from host state
        adapter._extract_tokens_from_logs = lambda: None  # type: ignore[assignment]
        stdout = self._load_fixture("copilot_normal.txt")
        result = self._make_copilot_result(stdout)
        output = adapter.parse_output(result, duration=1.0)

        assert "authenticate" in output.stdout
        assert output.cost_model == "per_token"
        assert output.cost_source in ("estimated", "calculated")
        assert output.output_tokens == 87
        assert output.input_tokens is not None  # estimated from stream content
        assert output.input_tokens > 0
        # Estimated cost from both input + output tokens (GPT-4o pricing)
        assert output.cost_usd is not None
        expected_cost = output.input_tokens * 2.50 / 1_000_000 + 87 * 10.0 / 1_000_000
        assert output.cost_usd == pytest.approx(expected_cost, abs=1e-8)
        assert output.error is None

    def test_ndjson_long_output(self) -> None:
        adapter = CopilotAdapter()
        stdout = self._load_fixture("copilot_long.txt")
        result = self._make_copilot_result(stdout)
        output = adapter.parse_output(result, duration=3.0)

        assert "refactoring" in output.stdout
        assert output.cost_model == "per_token"
        assert output.cost_source in ("estimated", "calculated")
        assert output.output_tokens == 312
        assert output.input_tokens is not None
        assert output.input_tokens > 0
        expected_cost = output.input_tokens * 2.50 / 1_000_000 + 312 * 10.0 / 1_000_000
        assert output.cost_usd == pytest.approx(expected_cost, abs=1e-8)
        assert output.error is None

    def test_ndjson_without_token_count_errors(self) -> None:
        adapter = CopilotAdapter()
        adapter._extract_tokens_from_logs = lambda: None  # type: ignore[assignment]
        stdout = self._load_fixture("copilot_no_tokens.txt")
        result = self._make_copilot_result(stdout)
        output = adapter.parse_output(result, duration=0.5)

        assert output.stdout == "Simple response without token count"
        assert output.error is not None
        assert "outputTokens" in output.error
        assert "1.0.4" in output.error
        assert output.output_tokens is None
        assert output.cost_source == "unavailable"

    def test_plain_text_errors_with_upgrade_hint(self) -> None:
        adapter = CopilotAdapter()
        adapter._extract_tokens_from_logs = lambda: None  # type: ignore[assignment]
        stdout = self._load_fixture("copilot_plain.txt")
        result = self._make_copilot_result(stdout)
        output = adapter.parse_output(result, duration=0.5)

        assert output.stdout == stdout
        assert output.error is not None
        assert "1.0.4" in output.error
        assert "upgrade" in output.error.lower()
        assert output.output_tokens is None
        assert output.cost_source == "unavailable"

    def test_empty_output_errors(self) -> None:
        adapter = CopilotAdapter()
        adapter._extract_tokens_from_logs = lambda: None  # type: ignore[assignment]
        stdout = self._load_fixture("copilot_empty.txt")
        result = self._make_copilot_result(stdout)
        output = adapter.parse_output(result, duration=0.5)

        assert output.error is not None
        assert output.output_tokens is None


class TestCopilotInputTokens:
    """Tests for Copilot input_tokens extraction from NDJSON usage events and process logs."""

    @staticmethod
    def _load_fixture(name: str) -> str:
        return (FIXTURE_DIR / name).read_text()

    def _make_copilot_result(self, stdout: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["copilot"], returncode=0, stdout=stdout, stderr="")

    def test_input_tokens_from_ndjson_usage_event(self) -> None:
        """When NDJSON stream contains a usage event with inputTokens, extract it."""
        adapter = CopilotAdapter()
        stdout = self._load_fixture("copilot_with_usage.txt")
        result = self._make_copilot_result(stdout)
        output = adapter.parse_output(result, duration=1.0)

        assert output.input_tokens == 1234
        assert output.output_tokens == 87
        assert output.cost_source in ("estimated", "calculated")
        assert output.cost_model == "per_token"
        assert output.cost_usd == pytest.approx(1234 * 2.50 / 1_000_000 + 87 * 10.0 / 1_000_000)
        assert output.error is None

    def test_input_tokens_estimated_from_stream_content(self) -> None:
        """When NDJSON has no usage event, input_tokens is estimated from stream chars."""
        adapter = CopilotAdapter()
        stdout = self._load_fixture("copilot_normal.txt")
        result = self._make_copilot_result(stdout)
        output = adapter.parse_output(result, duration=1.0)

        assert output.input_tokens is not None
        assert output.input_tokens > 0
        assert output.output_tokens == 87
        assert output.cost_source in ("estimated", "calculated")

    def test_input_tokens_always_estimated_without_usage_event(self) -> None:
        """Without a usage event, input_tokens is estimated from assistant content chars."""
        adapter = CopilotAdapter()
        stdout = self._load_fixture("copilot_normal.txt")
        result = self._make_copilot_result(stdout)
        output = adapter.parse_output(result, duration=1.0)

        assert output.input_tokens is not None
        assert output.output_tokens == 87
        assert output.cost_source in ("estimated", "calculated")
        expected_cost = output.input_tokens * 2.50 / 1_000_000 + 87 * 10.0 / 1_000_000
        assert output.cost_usd == pytest.approx(expected_cost, abs=1e-8)
        assert output.error is None

    def test_input_tokens_from_result_event(self) -> None:
        """When NDJSON has no usage event but result event has token counts, extract them."""
        adapter = CopilotAdapter()
        stdout = self._load_fixture("copilot_result_tokens.txt")
        result = self._make_copilot_result(stdout)
        output = adapter.parse_output(result, duration=1.0)

        assert output.input_tokens == 1500
        assert output.output_tokens == 393
        assert output.cost_source in ("estimated", "calculated")
        assert output.cost_usd == pytest.approx(1500 * 2.50 / 1_000_000 + 393 * 10.0 / 1_000_000)
        assert output.error is None

    def test_input_tokens_prefers_usage_event_over_result(self) -> None:
        """Usage event inputTokens takes priority over result event usage block."""
        adapter = CopilotAdapter()
        stdout = self._load_fixture("copilot_with_usage.txt")
        result = self._make_copilot_result(stdout)
        output = adapter.parse_output(result, duration=1.0)

        # copilot_with_usage.txt has usage event with inputTokens=1234
        assert output.input_tokens == 1234
        assert output.output_tokens == 87
        assert output.cost_source in ("estimated", "calculated")
        assert output.cost_usd == pytest.approx(1234 * 2.50 / 1_000_000 + 87 * 10.0 / 1_000_000)

    def test_result_event_prompt_tokens_fallback(self) -> None:
        """Result event with prompt_tokens/completion_tokens keys (OpenAI naming)."""
        adapter = CopilotAdapter()
        stdout = (
            '{"type":"assistant.message","data":{"content":"hi"}}\n'
            '{"type":"result","data":{"content":"done"},'
            '"usage":{"prompt_tokens":800,"completion_tokens":200}}\n'
        )
        result = self._make_copilot_result(stdout)
        output = adapter.parse_output(result, duration=1.0)

        assert output.input_tokens == 800
        assert output.output_tokens == 200
        assert output.cost_source in ("estimated", "calculated")
        assert output.cost_usd == pytest.approx(800 * 2.50 / 1_000_000 + 200 * 10.0 / 1_000_000)


# -- CodexAdapter --------------------------------------------------------------


def _mock_openai_module(**overrides: object) -> MagicMock:
    """Build a mock openai module with all exception types pre-configured.

    Pass ``client=<mock>`` to set the return value of ``openai.OpenAI()``.
    """
    m = MagicMock()
    m.NotFoundError = type("NotFoundError", (Exception,), {})
    m.AuthenticationError = type("AuthenticationError", (Exception,), {})
    m.RateLimitError = type("RateLimitError", (Exception,), {})
    m.APIError = type("APIError", (Exception,), {})
    if "client" in overrides:
        m.OpenAI.return_value = overrides.pop("client")
    for k, v in overrides.items():
        setattr(m, k, v)
    return m


class TestCodexAdapter:
    def test_is_agent_adapter(self) -> None:
        from codeprobe.adapters.codex import CodexAdapter

        adapter = CodexAdapter()
        assert isinstance(adapter, AgentAdapter)

    def test_name(self) -> None:
        from codeprobe.adapters.codex import CodexAdapter

        assert CodexAdapter().name == "codex"

    def test_preflight_with_api_key(self) -> None:
        from codeprobe.adapters.codex import CodexAdapter

        adapter = CodexAdapter()
        config = AgentConfig()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            with patch.dict("sys.modules", {"openai": MagicMock()}):
                issues = adapter.preflight(config)
        assert issues == []

    def test_preflight_missing_api_key(self) -> None:
        from codeprobe.adapters.codex import CodexAdapter

        adapter = CodexAdapter()
        config = AgentConfig()
        with patch.dict("os.environ", {}, clear=True):
            with patch.dict("sys.modules", {"openai": MagicMock()}):
                issues = adapter.preflight(config)
        assert any("OPENAI_API_KEY" in i for i in issues)

    def test_preflight_openai_not_installed(self) -> None:
        from codeprobe.adapters.codex import CodexAdapter

        adapter = CodexAdapter()
        config = AgentConfig()
        with patch.dict("sys.modules", {"openai": None}):
            issues = adapter.preflight(config)
        assert any("openai" in i.lower() for i in issues)

    def test_run_success(self) -> None:
        from codeprobe.adapters.codex import CodexAdapter

        mock_usage = MagicMock()
        mock_usage.input_tokens = 1000
        mock_usage.output_tokens = 500

        mock_response = MagicMock()
        mock_response.output_text = "Fixed the bug"
        mock_response.usage = mock_usage

        mock_client_instance = MagicMock()
        mock_client_instance.responses.create.return_value = mock_response

        mock_openai = _mock_openai_module(client=mock_client_instance)

        with patch.dict("sys.modules", {"openai": mock_openai}):
            adapter = CodexAdapter()
            config = AgentConfig()
            output = adapter.run("fix the bug", config)

        assert output.stdout == "Fixed the bug"
        assert output.input_tokens == 1000
        assert output.output_tokens == 500
        # codex-mini: $1.50/1M input + $6.00/1M output
        expected_cost = 1000 * 1.50 / 1_000_000 + 500 * 6.00 / 1_000_000
        assert output.cost_usd == pytest.approx(expected_cost)
        assert output.cost_model == "per_token"
        assert output.cost_source == "calculated"
        assert output.exit_code == 0
        assert output.duration_seconds > 0

    def test_run_auth_error(self) -> None:
        from codeprobe.adapters.codex import CodexAdapter

        mock_openai = _mock_openai_module()
        mock_client_instance = MagicMock()
        mock_client_instance.responses.create.side_effect = mock_openai.AuthenticationError("invalid key")
        mock_openai.OpenAI.return_value = mock_client_instance

        with patch.dict("sys.modules", {"openai": mock_openai}):
            adapter = CodexAdapter()
            config = AgentConfig()
            with pytest.raises(AdapterSetupError, match="OPENAI_API_KEY"):
                adapter.run("test", config)

    def test_run_rate_limit_error(self) -> None:
        from codeprobe.adapters.codex import CodexAdapter

        mock_openai = _mock_openai_module()
        mock_client_instance = MagicMock()
        mock_client_instance.responses.create.side_effect = mock_openai.RateLimitError("too many requests")
        mock_openai.OpenAI.return_value = mock_client_instance

        with patch.dict("sys.modules", {"openai": mock_openai}):
            adapter = CodexAdapter()
            config = AgentConfig()
            with pytest.raises(AdapterExecutionError, match="Rate limited"):
                adapter.run("test", config)

    def test_run_api_error(self) -> None:
        from codeprobe.adapters.codex import CodexAdapter

        mock_openai = _mock_openai_module()
        mock_client_instance = MagicMock()
        mock_client_instance.responses.create.side_effect = mock_openai.APIError("server error")
        mock_openai.OpenAI.return_value = mock_client_instance

        with patch.dict("sys.modules", {"openai": mock_openai}):
            adapter = CodexAdapter()
            config = AgentConfig()
            with pytest.raises(AdapterExecutionError, match="OpenAI API error"):
                adapter.run("test", config)

    def test_run_openai_not_installed(self) -> None:
        from codeprobe.adapters.codex import CodexAdapter

        adapter = CodexAdapter()
        config = AgentConfig()
        with patch.dict("sys.modules", {"openai": None}):
            with pytest.raises(AdapterSetupError, match="pip install"):
                adapter.run("test", config)

    def test_run_no_usage_returns_error(self) -> None:
        from codeprobe.adapters.codex import CodexAdapter

        mock_response = MagicMock()
        mock_response.output_text = "response text"
        mock_response.usage = None

        mock_client_instance = MagicMock()
        mock_client_instance.responses.create.return_value = mock_response
        mock_openai = _mock_openai_module(client=mock_client_instance)

        with patch.dict("sys.modules", {"openai": mock_openai}):
            adapter = CodexAdapter()
            config = AgentConfig()
            output = adapter.run("test", config)

        assert output.stdout == "response text"
        assert output.error is not None
        assert "usage" in output.error.lower()
        assert output.cost_usd is None
        assert output.cost_source == "unavailable"

    def test_run_unknown_model_no_cost(self) -> None:
        from codeprobe.adapters.codex import CodexAdapter

        mock_usage = MagicMock()
        mock_usage.input_tokens = 1000
        mock_usage.output_tokens = 500

        mock_response = MagicMock()
        mock_response.output_text = "ok"
        mock_response.usage = mock_usage

        mock_client_instance = MagicMock()
        mock_client_instance.responses.create.return_value = mock_response
        mock_openai = _mock_openai_module(client=mock_client_instance)

        with patch.dict("sys.modules", {"openai": mock_openai}):
            adapter = CodexAdapter()
            config = AgentConfig(model="gpt-4o")
            output = adapter.run("test", config)

        assert output.input_tokens == 1000
        assert output.output_tokens == 500
        assert output.cost_usd is None
        assert output.cost_model == "unknown"
        assert output.cost_source == "unavailable"

    def test_run_responses_api_fallback_to_chat_completions(self) -> None:
        """When responses.create raises NotFoundError, fall back to chat.completions."""
        from codeprobe.adapters.codex import CodexAdapter

        mock_chat_usage = MagicMock()
        mock_chat_usage.prompt_tokens = 800
        mock_chat_usage.completion_tokens = 400

        mock_message = MagicMock()
        mock_message.content = "Fixed via chat completions"

        mock_choice = MagicMock()
        mock_choice.message = mock_message

        mock_chat_response = MagicMock()
        mock_chat_response.choices = [mock_choice]
        mock_chat_response.usage = mock_chat_usage

        mock_client_instance = MagicMock()
        mock_openai = _mock_openai_module(client=mock_client_instance)
        mock_client_instance.responses.create.side_effect = mock_openai.NotFoundError("model not found")
        mock_client_instance.chat.completions.create.return_value = mock_chat_response

        with patch.dict("sys.modules", {"openai": mock_openai}):
            adapter = CodexAdapter()
            config = AgentConfig()
            output = adapter.run("fix the bug", config)

        assert output.stdout == "Fixed via chat completions"
        assert output.input_tokens == 800
        assert output.output_tokens == 400
        expected_cost = 800 * 1.50 / 1_000_000 + 400 * 6.00 / 1_000_000
        assert output.cost_usd == pytest.approx(expected_cost)
        assert output.cost_model == "per_token"
        assert output.cost_source == "calculated"
        assert output.exit_code == 0

    def test_run_chat_completions_auth_error(self) -> None:
        """Auth errors from the chat completions fallback path raise AdapterSetupError."""
        from codeprobe.adapters.codex import CodexAdapter

        mock_client_instance = MagicMock()
        mock_openai = _mock_openai_module(client=mock_client_instance)
        mock_client_instance.responses.create.side_effect = mock_openai.NotFoundError("not found")
        mock_client_instance.chat.completions.create.side_effect = mock_openai.AuthenticationError("bad key")

        with patch.dict("sys.modules", {"openai": mock_openai}):
            adapter = CodexAdapter()
            config = AgentConfig()
            with pytest.raises(AdapterSetupError, match="OPENAI_API_KEY"):
                adapter.run("test", config)

    def test_run_chat_completions_rate_limit_error(self) -> None:
        """Rate limit errors from the chat completions fallback raise AdapterExecutionError."""
        from codeprobe.adapters.codex import CodexAdapter

        mock_client_instance = MagicMock()
        mock_openai = _mock_openai_module(client=mock_client_instance)
        mock_client_instance.responses.create.side_effect = mock_openai.NotFoundError("not found")
        mock_client_instance.chat.completions.create.side_effect = mock_openai.RateLimitError("too many")

        with patch.dict("sys.modules", {"openai": mock_openai}):
            adapter = CodexAdapter()
            config = AgentConfig()
            with pytest.raises(AdapterExecutionError, match="Rate limited"):
                adapter.run("test", config)

    def test_run_double_not_found_error(self) -> None:
        """Model not on either API raises AdapterExecutionError with clear message."""
        from codeprobe.adapters.codex import CodexAdapter

        mock_client_instance = MagicMock()
        mock_openai = _mock_openai_module(client=mock_client_instance)
        mock_client_instance.responses.create.side_effect = mock_openai.NotFoundError("not on responses")
        mock_client_instance.chat.completions.create.side_effect = mock_openai.NotFoundError("not on chat either")

        with patch.dict("sys.modules", {"openai": mock_openai}):
            adapter = CodexAdapter()
            config = AgentConfig()
            with pytest.raises(AdapterExecutionError, match="not available on Responses or"):
                adapter.run("test", config)

    def test_run_chat_fallback_no_usage(self) -> None:
        """Chat Completions fallback with no usage data returns error in output."""
        from codeprobe.adapters.codex import CodexAdapter

        mock_message = MagicMock()
        mock_message.content = "response without usage"

        mock_choice = MagicMock()
        mock_choice.message = mock_message

        mock_chat_response = MagicMock()
        mock_chat_response.choices = [mock_choice]
        mock_chat_response.usage = None

        mock_client_instance = MagicMock()
        mock_openai = _mock_openai_module(client=mock_client_instance)
        mock_client_instance.responses.create.side_effect = mock_openai.NotFoundError("model not found")
        mock_client_instance.chat.completions.create.return_value = mock_chat_response

        with patch.dict("sys.modules", {"openai": mock_openai}):
            adapter = CodexAdapter()
            config = AgentConfig()
            output = adapter.run("test", config)

        assert output.stdout == "response without usage"
        assert output.error is not None
        assert "usage" in output.error.lower()
        assert output.cost_usd is None

    def test_run_chat_fallback_empty_choices(self) -> None:
        """Chat Completions fallback with empty choices returns empty stdout."""
        from codeprobe.adapters.codex import CodexAdapter

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 500
        mock_usage.completion_tokens = 200

        mock_chat_response = MagicMock()
        mock_chat_response.choices = []
        mock_chat_response.usage = mock_usage

        mock_client_instance = MagicMock()
        mock_openai = _mock_openai_module(client=mock_client_instance)
        mock_client_instance.responses.create.side_effect = mock_openai.NotFoundError("model not found")
        mock_client_instance.chat.completions.create.return_value = mock_chat_response

        with patch.dict("sys.modules", {"openai": mock_openai}):
            adapter = CodexAdapter()
            config = AgentConfig()
            output = adapter.run("test", config)

        assert output.stdout == ""
        assert output.input_tokens == 500
        assert output.output_tokens == 200

    def test_run_chat_fallback_codex_latest_pricing(self) -> None:
        """Chat Completions fallback with codex-latest model uses correct pricing."""
        from codeprobe.adapters.codex import CodexAdapter

        mock_chat_usage = MagicMock()
        mock_chat_usage.prompt_tokens = 1000
        mock_chat_usage.completion_tokens = 500

        mock_message = MagicMock()
        mock_message.content = "codex-latest response"

        mock_choice = MagicMock()
        mock_choice.message = mock_message

        mock_chat_response = MagicMock()
        mock_chat_response.choices = [mock_choice]
        mock_chat_response.usage = mock_chat_usage

        mock_client_instance = MagicMock()
        mock_openai = _mock_openai_module(client=mock_client_instance)
        mock_client_instance.responses.create.side_effect = mock_openai.NotFoundError("model not found")
        mock_client_instance.chat.completions.create.return_value = mock_chat_response

        with patch.dict("sys.modules", {"openai": mock_openai}):
            adapter = CodexAdapter()
            config = AgentConfig(model="codex-latest")
            output = adapter.run("test", config)

        assert output.stdout == "codex-latest response"
        assert output.input_tokens == 1000
        assert output.output_tokens == 500
        # codex-latest: $2.00/1M input + $8.00/1M output
        expected_cost = 1000 * 2.00 / 1_000_000 + 500 * 8.00 / 1_000_000
        assert output.cost_usd == pytest.approx(expected_cost)
        assert output.cost_model == "per_token"
        assert output.cost_source == "calculated"


# -- AiderAdapter --------------------------------------------------------------


# -- MCP config wiring ---------------------------------------------------------


class TestCopilotMcpConfig:
    def test_build_command_includes_mcp_flag(self) -> None:
        adapter = CopilotAdapter()
        mcp = {"servers": {"fetch": {"command": "fetch-mcp"}}}
        config = AgentConfig(mcp_config=mcp)
        if adapter.find_binary():
            cmd = adapter.build_command("test", config)
            assert "--additional-mcp-config" in cmd
            idx = cmd.index("--additional-mcp-config")
            arg = cmd[idx + 1]
            assert arg.startswith("@"), "Copilot MCP config must use @filepath syntax"
            path = arg[1:]  # strip @ prefix
            with open(path) as f:
                payload = json.load(f)
            assert payload == mcp

    def test_build_command_omits_mcp_flag_when_none(self) -> None:
        adapter = CopilotAdapter()
        config = AgentConfig(mcp_config=None)
        if adapter.find_binary():
            cmd = adapter.build_command("test", config)
            assert "--additional-mcp-config" not in cmd

    def test_build_command_omits_mcp_flag_when_empty(self) -> None:
        adapter = CopilotAdapter()
        config = AgentConfig(mcp_config={})
        if adapter.find_binary():
            cmd = adapter.build_command("test", config)
            assert "--additional-mcp-config" not in cmd

    def test_preflight_no_longer_warns_on_mcp(self) -> None:
        adapter = CopilotAdapter()
        config = AgentConfig(mcp_config={"servers": {"s": {}}})
        issues = adapter.preflight(config)
        mcp_warnings = [i for i in issues if "MCP" in i]
        assert len(mcp_warnings) == 0


class TestClaudeMcpConfig:
    def test_build_command_includes_mcp_flag(self) -> None:
        adapter = ClaudeAdapter()
        mcp = {"servers": {"fetch": {"command": "fetch-mcp"}}}
        config = AgentConfig(mcp_config=mcp)
        if adapter.find_binary():
            cmd = adapter.build_command("test", config)
            assert "--mcp-config" in cmd
            idx = cmd.index("--mcp-config")
            path = cmd[idx + 1]
            with open(path) as f:
                payload = json.load(f)
            assert payload == mcp

    def test_build_command_omits_mcp_flag_when_none(self) -> None:
        adapter = ClaudeAdapter()
        config = AgentConfig(mcp_config=None)
        if adapter.find_binary():
            cmd = adapter.build_command("test", config)
            assert "--mcp-config" not in cmd

    def test_build_command_omits_mcp_flag_when_empty(self) -> None:
        adapter = ClaudeAdapter()
        config = AgentConfig(mcp_config={})
        if adapter.find_binary():
            cmd = adapter.build_command("test", config)
            assert "--mcp-config" not in cmd


# -- isolate_session() tests --------------------------------------------------


class TestIsolateSession:
    def test_claude_isolate_session_returns_config_dir(self, tmp_path: Path) -> None:
        adapter = ClaudeAdapter()
        fake_home = tmp_path / "home"
        (fake_home / ".claude").mkdir(parents=True)
        (fake_home / ".claude" / ".credentials.json").write_text("{}")
        with (
            patch.object(Path, "home", return_value=fake_home),
            patch(
                "codeprobe.adapters.claude.tempfile.gettempdir",
                return_value=str(tmp_path / "tmp"),
            ),
        ):
            env = adapter.isolate_session(0)
        assert "CLAUDE_CONFIG_DIR" in env
        assert "slot-0" in env["CLAUDE_CONFIG_DIR"]

    def test_claude_isolate_session_different_slots(self, tmp_path: Path) -> None:
        adapter = ClaudeAdapter()
        fake_home = tmp_path / "home"
        (fake_home / ".claude").mkdir(parents=True)
        (fake_home / ".claude" / ".credentials.json").write_text("{}")
        with (
            patch.object(Path, "home", return_value=fake_home),
            patch(
                "codeprobe.adapters.claude.tempfile.gettempdir",
                return_value=str(tmp_path / "tmp"),
            ),
        ):
            env0 = adapter.isolate_session(0)
            env1 = adapter.isolate_session(1)
        assert env0["CLAUDE_CONFIG_DIR"] != env1["CLAUDE_CONFIG_DIR"]

    def test_claude_isolate_session_skips_when_no_creds(self, tmp_path: Path) -> None:
        """When no credential files exist, returns empty dict so keychain auth still works."""
        adapter = ClaudeAdapter()
        fake_home = tmp_path / "home"
        (fake_home / ".claude").mkdir(parents=True)
        with patch.object(Path, "home", return_value=fake_home):
            env = adapter.isolate_session(0)
        assert env == {}

    def test_base_adapter_isolate_session_returns_empty(self) -> None:
        adapter = _StubAdapter()
        env = adapter.isolate_session(42)
        assert env == {}

    def test_copilot_isolate_session_returns_empty(self) -> None:
        adapter = CopilotAdapter()
        env = adapter.isolate_session(0)
        assert env == {}

    def test_claude_isolate_session_symlinks_credentials_live(self, tmp_path: Path) -> None:
        """Creds file is symlinked so OAuth refreshes propagate across slots."""
        adapter = ClaudeAdapter()

        fake_home = tmp_path / "home"
        real_claude = fake_home / ".claude"
        real_claude.mkdir(parents=True)
        cred_file = real_claude / ".credentials.json"
        cred_file.write_text('{"token": "v1"}', encoding="utf-8")

        with (
            patch.object(Path, "home", return_value=fake_home),
            patch(
                "codeprobe.adapters.claude.tempfile.gettempdir",
                return_value=str(tmp_path / "tmp"),
            ),
        ):
            adapter.isolate_session(0)
            adapter.isolate_session(1)

        slot0_cred = tmp_path / "tmp" / "codeprobe-claude" / "slot-0" / ".credentials.json"
        slot1_cred = tmp_path / "tmp" / "codeprobe-claude" / "slot-1" / ".credentials.json"
        assert slot0_cred.is_symlink()
        assert slot1_cred.is_symlink()

        # Refreshing the live creds is visible in every slot — no stale copies.
        cred_file.write_text('{"token": "v2"}', encoding="utf-8")
        assert '"v2"' in slot0_cred.read_text()
        assert '"v2"' in slot1_cred.read_text()

    def test_claude_isolate_session_mutable_dirs_are_fresh(self, tmp_path: Path) -> None:
        """Per-session mutable state (session-env/, sessions/, history.jsonl)
        is isolated per slot so parallel workers never race on shared writes.
        """
        adapter = ClaudeAdapter()

        fake_home = tmp_path / "home"
        real_claude = fake_home / ".claude"
        real_claude.mkdir(parents=True)
        (real_claude / ".credentials.json").write_text("{}")

        # Seed shared mutable dirs & file in the real config; these MUST NOT
        # leak into slot dirs (they are per-session, not per-machine).
        shared_session_env = real_claude / "session-env"
        shared_session_env.mkdir()
        (shared_session_env / "parent.json").write_text('{"from": "parent"}')
        (real_claude / "history.jsonl").write_text("{}\n")

        with (
            patch.object(Path, "home", return_value=fake_home),
            patch(
                "codeprobe.adapters.claude.tempfile.gettempdir",
                return_value=str(tmp_path / "tmp"),
            ),
        ):
            adapter.isolate_session(0)
            adapter.isolate_session(1)

        slot0 = tmp_path / "tmp" / "codeprobe-claude" / "slot-0"
        slot1 = tmp_path / "tmp" / "codeprobe-claude" / "slot-1"

        # Mutable dir exists, is NOT a symlink, and is empty.
        for slot in (slot0, slot1):
            se = slot / "session-env"
            assert se.is_dir()
            assert not se.is_symlink()
            assert list(se.iterdir()) == []
            hist = slot / "history.jsonl"
            assert hist.is_file()
            assert not hist.is_symlink()
            assert hist.read_text() == ""

    def test_claude_isolate_session_mirrors_read_only_entries_as_symlinks(self, tmp_path: Path) -> None:
        """Settings, skills, and other read-only config are symlinked so
        CLI configuration stays consistent with the real home directory.
        """
        adapter = ClaudeAdapter()
        fake_home = tmp_path / "home"
        real_claude = fake_home / ".claude"
        real_claude.mkdir(parents=True)
        (real_claude / ".credentials.json").write_text("{}")
        (real_claude / "settings.json").write_text('{"theme": "dark"}')
        (real_claude / "skills").mkdir()
        (real_claude / "skills" / "example.md").write_text("# skill")

        with (
            patch.object(Path, "home", return_value=fake_home),
            patch(
                "codeprobe.adapters.claude.tempfile.gettempdir",
                return_value=str(tmp_path / "tmp"),
            ),
        ):
            adapter.isolate_session(0)

        slot0 = tmp_path / "tmp" / "codeprobe-claude" / "slot-0"
        assert (slot0 / "settings.json").is_symlink()
        assert '"dark"' in (slot0 / "settings.json").read_text()
        assert (slot0 / "skills").is_symlink()
        assert (slot0 / "skills" / "example.md").read_text() == "# skill"

    def test_claude_isolate_session_refreshes_stale_mirror(self, tmp_path: Path) -> None:
        """Symlinks are re-pointed when the live config entry changes
        (e.g. settings regenerated, new skill added) so a stale slot dir
        from a previous run doesn't silently drift out of sync.
        """
        adapter = ClaudeAdapter()
        fake_home = tmp_path / "home"
        real_claude = fake_home / ".claude"
        real_claude.mkdir(parents=True)
        (real_claude / ".credentials.json").write_text("{}")
        settings = real_claude / "settings.json"
        settings.write_text('{"v": 1}')

        with (
            patch.object(Path, "home", return_value=fake_home),
            patch(
                "codeprobe.adapters.claude.tempfile.gettempdir",
                return_value=str(tmp_path / "tmp"),
            ),
        ):
            # Seed a stale symlink pointing to a now-deleted file.
            adapter.isolate_session(0)
            slot0 = tmp_path / "tmp" / "codeprobe-claude" / "slot-0"
            assert (slot0 / "settings.json").is_symlink()

            # Live settings rewritten — mirror should still resolve because
            # we symlinked to the path, not copied content.
            settings.write_text('{"v": 2}')
            assert '"v": 2' in (slot0 / "settings.json").read_text()

            # Re-isolate after live file is deleted — slot dir follows
            # reality (stale entry cleaned up).
            settings.unlink()
            (real_claude / "settings.json").write_text('{"v": 3}')
            adapter.isolate_session(0)
            assert '"v": 3' in (slot0 / "settings.json").read_text()


class TestCheckParallelAuth:
    def test_ok_when_parallel_one(self) -> None:
        assert ClaudeAdapter.check_parallel_auth(1) is None

    def test_ok_with_file_credentials(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_home = tmp_path / "home"
        (fake_home / ".claude").mkdir(parents=True)
        (fake_home / ".claude" / ".credentials.json").write_text("{}")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        with patch.object(Path, "home", return_value=fake_home):
            assert ClaudeAdapter.check_parallel_auth(3) is None

    def test_ok_with_env_var_auth(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_home = tmp_path / "home"
        (fake_home / ".claude").mkdir(parents=True)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        with patch.object(Path, "home", return_value=fake_home):
            assert ClaudeAdapter.check_parallel_auth(3) is None

    def test_warns_when_parallel_without_any_auth_source(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_home = tmp_path / "home"
        (fake_home / ".claude").mkdir(parents=True)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        with patch.object(Path, "home", return_value=fake_home):
            warning = ClaudeAdapter.check_parallel_auth(3)
        assert warning is not None
        assert "--parallel 1" in warning
        assert "401" in warning

    def test_base_adapter_run_passes_session_env_to_subprocess(self) -> None:
        """session_env passed to run() reaches subprocess.run() via _adapter_safe_env."""
        adapter = ClaudeAdapter()
        config = AgentConfig(timeout_seconds=10)
        session_env = {"CLAUDE_CONFIG_DIR": "/tmp/codeprobe-claude/slot-0"}

        fake_result = MagicMock()
        fake_result.stdout = '{"result": "ok"}'
        fake_result.stderr = ""
        fake_result.returncode = 0

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch("subprocess.run", return_value=fake_result) as mock_run,
        ):
            adapter.run("test prompt", config, session_env=session_env)

        # Verify CLAUDE_CONFIG_DIR was passed in the env dict
        call_kwargs = mock_run.call_args
        env_passed = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
        assert env_passed is not None
        assert env_passed["CLAUDE_CONFIG_DIR"] == "/tmp/codeprobe-claude/slot-0"


# ---------------------------------------------------------------------------
# Loud fallbacks — NDJSON parse fallback sets error + emits warning
# ---------------------------------------------------------------------------


class TestCopilotNdjsonFallback:
    """When NDJSON parsing fails, error field is set and WARNING is logged."""

    def _make_copilot_result(self, stdout: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["copilot"], returncode=0, stdout=stdout, stderr="")

    def test_ndjson_fallback_sets_error_field(self) -> None:
        """Non-JSON stdout triggers fallback with error containing 'ndjson_parse_fallback'."""
        adapter = CopilotAdapter()
        adapter._extract_tokens_from_logs = lambda: None  # type: ignore[assignment]
        raw_text = "This is plain text, not NDJSON"
        result = self._make_copilot_result(raw_text)
        output = adapter.parse_output(result, duration=1.0)

        assert output.stdout == raw_text
        assert output.error is not None
        assert "ndjson_parse_fallback" in output.error

    def test_ndjson_fallback_emits_warning_log(self, caplog: pytest.LogCaptureFixture) -> None:
        """WARNING log is emitted when NDJSON parsing falls back."""
        import logging

        adapter = CopilotAdapter()
        adapter._extract_tokens_from_logs = lambda: None  # type: ignore[assignment]
        raw_text = "Plain text output, not valid JSON"
        result = self._make_copilot_result(raw_text)

        with caplog.at_level(logging.WARNING, logger="codeprobe.adapters.copilot"):
            adapter.parse_output(result, duration=1.0)

        assert any("ndjson_parse_fallback" in rec.message for rec in caplog.records)

    def test_ndjson_fallback_catches_json_decode_error_only(self) -> None:
        """Verify only json.JSONDecodeError is caught, not broad ValueError."""
        adapter = CopilotAdapter()
        adapter._extract_tokens_from_logs = lambda: None  # type: ignore[assignment]
        # Malformed JSON triggers JSONDecodeError — should be caught
        raw_text = '{"type": "assistant.message"'  # missing closing brace
        result = self._make_copilot_result(raw_text)
        output = adapter.parse_output(result, duration=1.0)

        assert output.error is not None
        assert "ndjson_parse_fallback" in output.error


# -- Sandbox detection ---------------------------------------------------------


from codeprobe.core.sandbox import is_sandboxed  # noqa: E402


class TestSandboxDetection:
    """Tests for is_sandboxed() — checks /.dockerenv, env var, and cgroup."""

    def test_sandboxed_via_dockerenv(self) -> None:
        with (
            patch("codeprobe.core.sandbox.Path") as mock_path_cls,
        ):
            dockerenv_path = MagicMock()
            dockerenv_path.exists.return_value = True

            def path_factory(p: str) -> MagicMock:
                return dockerenv_path

            mock_path_cls.side_effect = path_factory
            env = {k: v for k, v in __import__("os").environ.items() if k != "CODEPROBE_SANDBOX"}
            with patch.dict("os.environ", env, clear=True):
                assert is_sandboxed() is True

    def test_sandboxed_via_env_var(self) -> None:
        with (
            patch("codeprobe.core.sandbox.Path") as mock_path_cls,
            patch.dict("os.environ", {"CODEPROBE_SANDBOX": "1"}, clear=False),
        ):
            dockerenv_path = MagicMock()
            dockerenv_path.exists.return_value = False

            def path_factory(p: str) -> MagicMock:
                return dockerenv_path

            mock_path_cls.side_effect = path_factory
            assert is_sandboxed() is True

    def test_sandboxed_via_cgroup_docker(self) -> None:
        with (
            patch("codeprobe.core.sandbox.Path") as mock_path_cls,
        ):
            dockerenv_path = MagicMock()
            dockerenv_path.exists.return_value = False
            cgroup_path = MagicMock()
            cgroup_path.read_text.return_value = "12:memory:/docker/abc123\n0::/system.slice/docker-abc.scope\n"

            def path_factory(p: str) -> MagicMock:
                if p == "/proc/1/cgroup":
                    return cgroup_path
                return dockerenv_path

            mock_path_cls.side_effect = path_factory
            env = {k: v for k, v in __import__("os").environ.items() if k != "CODEPROBE_SANDBOX"}
            with patch.dict("os.environ", env, clear=True):
                assert is_sandboxed() is True

    def test_sandboxed_via_cgroup_containerd(self) -> None:
        with (
            patch("codeprobe.core.sandbox.Path") as mock_path_cls,
        ):
            dockerenv_path = MagicMock()
            dockerenv_path.exists.return_value = False
            cgroup_path = MagicMock()
            cgroup_path.read_text.return_value = "0::/system.slice/containerd.service\n"

            def path_factory(p: str) -> MagicMock:
                if p == "/proc/1/cgroup":
                    return cgroup_path
                return dockerenv_path

            mock_path_cls.side_effect = path_factory
            env = {k: v for k, v in __import__("os").environ.items() if k != "CODEPROBE_SANDBOX"}
            with patch.dict("os.environ", env, clear=True):
                assert is_sandboxed() is True

    def test_not_sandboxed_bare_host(self) -> None:
        with (
            patch("codeprobe.core.sandbox.Path") as mock_path_cls,
        ):
            dockerenv_path = MagicMock()
            dockerenv_path.exists.return_value = False
            cgroup_path = MagicMock()
            cgroup_path.read_text.return_value = "0::/init.scope\n"

            def path_factory(p: str) -> MagicMock:
                if p == "/proc/1/cgroup":
                    return cgroup_path
                return dockerenv_path

            mock_path_cls.side_effect = path_factory
            env = {k: v for k, v in __import__("os").environ.items() if k != "CODEPROBE_SANDBOX"}
            with patch.dict("os.environ", env, clear=True):
                assert is_sandboxed() is False

    def test_not_sandboxed_cgroup_unreadable(self) -> None:
        with (
            patch("codeprobe.core.sandbox.Path") as mock_path_cls,
        ):
            dockerenv_path = MagicMock()
            dockerenv_path.exists.return_value = False
            cgroup_path = MagicMock()
            cgroup_path.read_text.side_effect = PermissionError

            def path_factory(p: str) -> MagicMock:
                if p == "/proc/1/cgroup":
                    return cgroup_path
                return dockerenv_path

            mock_path_cls.side_effect = path_factory
            env = {k: v for k, v in __import__("os").environ.items() if k != "CODEPROBE_SANDBOX"}
            with patch.dict("os.environ", env, clear=True):
                assert is_sandboxed() is False


class TestClaudeSandboxGating:
    """Tests for sandbox gating of dangerously_skip permission mode."""

    def test_preflight_rejects_dangerously_skip_outside_sandbox(self) -> None:
        adapter = ClaudeAdapter()
        config = AgentConfig(permission_mode="dangerously_skip")
        with (
            patch.object(adapter, "find_binary", return_value="/usr/bin/claude"),
            patch("codeprobe.adapters.claude.is_sandboxed", return_value=False),
        ):
            issues = adapter.preflight(config)
        assert any("sandboxed environment" in i for i in issues)

    def test_preflight_allows_dangerously_skip_in_sandbox(self) -> None:
        adapter = ClaudeAdapter()
        config = AgentConfig(permission_mode="dangerously_skip")
        with (
            patch.object(adapter, "find_binary", return_value="/usr/bin/claude"),
            patch("codeprobe.adapters.claude.is_sandboxed", return_value=True),
        ):
            issues = adapter.preflight(config)
        sandbox_issues = [i for i in issues if "sandboxed environment" in i]
        assert sandbox_issues == []

    def test_build_command_includes_skip_flag_in_sandbox(self) -> None:
        adapter = ClaudeAdapter()
        config = AgentConfig(permission_mode="dangerously_skip")
        with patch("shutil.which", return_value="/usr/bin/claude"):
            cmd = adapter.build_command("test", config)
        assert "--dangerously-skip-permissions" in cmd
        assert "--permission-mode" not in cmd

    def test_build_command_normal_mode_unchanged(self) -> None:
        adapter = ClaudeAdapter()
        config = AgentConfig(permission_mode="plan")
        with patch("shutil.which", return_value="/usr/bin/claude"):
            cmd = adapter.build_command("test", config)
        assert "--permission-mode" in cmd
        idx = cmd.index("--permission-mode")
        assert cmd[idx + 1] == "plan"
        assert "--dangerously-skip-permissions" not in cmd

    def test_dangerously_skip_in_allowed_modes(self) -> None:
        from codeprobe.adapters.protocol import ALLOWED_PERMISSION_MODES

        assert "dangerously_skip" in ALLOWED_PERMISSION_MODES


class TestClaudeModelNormalization:
    """Tests for model name normalization in Claude adapter."""

    def test_strips_date_suffix(self) -> None:
        from codeprobe.adapters.claude import _normalize_model_for_cli

        assert _normalize_model_for_cli("claude-sonnet-4-6-20250514") == "claude-sonnet-4-6"
        assert _normalize_model_for_cli("claude-opus-4-6-20250514") == "claude-opus-4-6"
        assert _normalize_model_for_cli("claude-haiku-4-5-20251001") == "claude-haiku-4-5"

    def test_preserves_aliases(self) -> None:
        from codeprobe.adapters.claude import _normalize_model_for_cli

        assert _normalize_model_for_cli("sonnet") == "sonnet"
        assert _normalize_model_for_cli("opus") == "opus"
        assert _normalize_model_for_cli("haiku") == "haiku"

    def test_preserves_short_ids(self) -> None:
        from codeprobe.adapters.claude import _normalize_model_for_cli

        assert _normalize_model_for_cli("claude-sonnet-4-6") == "claude-sonnet-4-6"

    def test_build_command_normalizes_model(self) -> None:
        adapter = ClaudeAdapter()
        config = AgentConfig(model="claude-sonnet-4-6-20250514")
        with patch("shutil.which", return_value="/usr/bin/claude"):
            cmd = adapter.build_command("test", config)
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "claude-sonnet-4-6"
