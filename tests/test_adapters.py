"""Tests for agent adapter protocol and implementations."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codeprobe.adapters._base import BaseAdapter
from codeprobe.adapters.aider import AiderAdapter
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
    assert config.timeout_seconds == 300


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
        output = AgentOutput(
            stdout="ok", stderr=None, exit_code=0, duration_seconds=1.0
        )
        assert output.input_tokens is None
        assert output.output_tokens is None
        assert output.cache_read_tokens is None
        assert output.cost_model == "unknown"

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
                return AgentOutput(
                    stdout="ok", stderr=None, exit_code=0, duration_seconds=0.1
                )

            def isolate_session(self, slot_id: int) -> dict[str, str]:
                return {}

        adapter = MinimalAdapter()
        assert isinstance(adapter, AgentAdapter)
        assert adapter.name == "minimal"


# -- AgentOutput error / cost_source fields ------------------------------------


class TestAgentOutputErrorField:
    def test_default_is_none(self) -> None:
        output = AgentOutput(
            stdout="ok", stderr=None, exit_code=0, duration_seconds=1.0
        )
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
        output = AgentOutput(
            stdout="ok", stderr=None, exit_code=0, duration_seconds=1.0
        )
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
    """Verify subprocess.run() uses a filtered environment, not full parent env."""

    def test_subprocess_receives_explicit_env(self) -> None:
        adapter = _StubAdapter()
        config = AgentConfig(timeout_seconds=5)
        fake_result = subprocess.CompletedProcess(
            args=["fake-agent"], returncode=0, stdout="ok", stderr=""
        )
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            adapter.run("test", config)

        _, kwargs = mock_run.call_args
        assert "env" in kwargs, "subprocess.run must receive explicit env"
        env = kwargs["env"]
        assert isinstance(env, dict)

    def test_env_includes_path_and_home(self) -> None:
        adapter = _StubAdapter()
        config = AgentConfig(timeout_seconds=5)
        fake_result = subprocess.CompletedProcess(
            args=["fake-agent"], returncode=0, stdout="ok", stderr=""
        )
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            import os

            old_path = os.environ.get("PATH", "")
            old_home = os.environ.get("HOME", "")
            adapter.run("test", config)

        env = mock_run.call_args[1]["env"]
        if old_path:
            assert env.get("PATH") == old_path
        if old_home:
            assert env.get("HOME") == old_home

    def test_env_excludes_random_secrets(self) -> None:
        adapter = _StubAdapter()
        config = AgentConfig(timeout_seconds=5)
        fake_result = subprocess.CompletedProcess(
            args=["fake-agent"], returncode=0, stdout="ok", stderr=""
        )
        import os

        os.environ["MY_SUPER_SECRET_DB_PASSWORD"] = "hunter2"
        try:
            with patch("subprocess.run", return_value=fake_result) as mock_run:
                adapter.run("test", config)
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
        assert output.error == "Agent timed out after 5s"
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
        with patch(
            "subprocess.run", side_effect=FileNotFoundError("/usr/bin/fake-agent")
        ):
            with pytest.raises(AdapterSetupError, match="Binary not found"):
                adapter.run("test", config)

    def test_require_binary_raises_setup_error(self) -> None:
        adapter = _StubAdapter()
        with patch.object(adapter, "find_binary", return_value=None):
            with pytest.raises(AdapterSetupError, match="CLI not found"):
                adapter._require_binary()


# -- BaseAdapter parse_output() ------------------------------------------------


class TestParseOutput:
    def test_default_maps_fields(self) -> None:
        adapter = _StubAdapter()
        result = subprocess.CompletedProcess(
            args=["fake-agent"], returncode=0, stdout="hello", stderr=""
        )
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

            def parse_output(
                self, result: subprocess.CompletedProcess[str], duration: float
            ) -> AgentOutput:
                raise ValueError("bad JSON")

        adapter = BrokenParser()
        config = AgentConfig()
        fake_result = subprocess.CompletedProcess(
            args=["broken"], returncode=0, stdout="raw output", stderr="err"
        )
        with patch("subprocess.run", return_value=fake_result):
            output = adapter.run("test", config)
        assert "Output parse failed" in output.error
        assert output.stdout == "raw output"


# -- ClaudeAdapter parse_output() ---------------------------------------------

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _make_result(fixture_name: str) -> subprocess.CompletedProcess[str]:
    """Load a fixture file and wrap it in a CompletedProcess."""
    content = (FIXTURE_DIR / fixture_name).read_text()
    return subprocess.CompletedProcess(
        args=["claude", "-p", "test"], returncode=0, stdout=content, stderr=""
    )


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


# -- CopilotAdapter parse_output() --------------------------------------------


class TestCopilotParseOutput:
    """Tests for CopilotAdapter.parse_output() — requires structured NDJSON from CLI 1.0.4+."""

    @staticmethod
    def _load_fixture(name: str) -> str:
        return (FIXTURE_DIR / name).read_text()

    def _make_copilot_result(self, stdout: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["copilot"], returncode=0, stdout=stdout, stderr=""
        )

    def test_ndjson_with_tokens(self) -> None:
        adapter = CopilotAdapter()
        # Mock log extraction to isolate from host state
        adapter._extract_tokens_from_logs = lambda: None  # type: ignore[assignment]
        stdout = self._load_fixture("copilot_normal.txt")
        result = self._make_copilot_result(stdout)
        output = adapter.parse_output(result, duration=1.0)

        assert "authenticate" in output.stdout
        assert output.cost_model == "per_token"
        assert output.cost_source == "estimated"
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
        assert output.cost_source == "estimated"
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
        return subprocess.CompletedProcess(
            args=["copilot"], returncode=0, stdout=stdout, stderr=""
        )

    def test_input_tokens_from_ndjson_usage_event(self) -> None:
        """When NDJSON stream contains a usage event with inputTokens, extract it."""
        adapter = CopilotAdapter()
        stdout = self._load_fixture("copilot_with_usage.txt")
        result = self._make_copilot_result(stdout)
        output = adapter.parse_output(result, duration=1.0)

        assert output.input_tokens == 1234
        assert output.output_tokens == 87
        assert output.cost_source == "estimated"
        assert output.cost_model == "per_token"
        assert output.cost_usd == pytest.approx(
            1234 * 2.50 / 1_000_000 + 87 * 10.0 / 1_000_000
        )
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
        assert output.cost_source == "estimated"

    def test_input_tokens_always_estimated_without_usage_event(self) -> None:
        """Without a usage event, input_tokens is estimated from assistant content chars."""
        adapter = CopilotAdapter()
        stdout = self._load_fixture("copilot_normal.txt")
        result = self._make_copilot_result(stdout)
        output = adapter.parse_output(result, duration=1.0)

        assert output.input_tokens is not None
        assert output.output_tokens == 87
        assert output.cost_source == "estimated"
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
        assert output.cost_source == "estimated"
        assert output.cost_usd == pytest.approx(
            1500 * 2.50 / 1_000_000 + 393 * 10.0 / 1_000_000
        )
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
        assert output.cost_source == "estimated"
        assert output.cost_usd == pytest.approx(
            1234 * 2.50 / 1_000_000 + 87 * 10.0 / 1_000_000
        )

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
        assert output.cost_source == "estimated"
        assert output.cost_usd == pytest.approx(
            800 * 2.50 / 1_000_000 + 200 * 10.0 / 1_000_000
        )


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
        mock_client_instance.responses.create.side_effect = (
            mock_openai.AuthenticationError("invalid key")
        )
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
        mock_client_instance.responses.create.side_effect = mock_openai.RateLimitError(
            "too many requests"
        )
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
        mock_client_instance.responses.create.side_effect = mock_openai.APIError(
            "server error"
        )
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
        mock_client_instance.responses.create.side_effect = mock_openai.NotFoundError(
            "model not found"
        )
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
        mock_client_instance.responses.create.side_effect = mock_openai.NotFoundError(
            "not found"
        )
        mock_client_instance.chat.completions.create.side_effect = (
            mock_openai.AuthenticationError("bad key")
        )

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
        mock_client_instance.responses.create.side_effect = mock_openai.NotFoundError(
            "not found"
        )
        mock_client_instance.chat.completions.create.side_effect = (
            mock_openai.RateLimitError("too many")
        )

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
        mock_client_instance.responses.create.side_effect = mock_openai.NotFoundError(
            "not on responses"
        )
        mock_client_instance.chat.completions.create.side_effect = (
            mock_openai.NotFoundError("not on chat either")
        )

        with patch.dict("sys.modules", {"openai": mock_openai}):
            adapter = CodexAdapter()
            config = AgentConfig()
            with pytest.raises(
                AdapterExecutionError, match="not available on Responses or"
            ):
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
        mock_client_instance.responses.create.side_effect = mock_openai.NotFoundError(
            "model not found"
        )
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
        mock_client_instance.responses.create.side_effect = mock_openai.NotFoundError(
            "model not found"
        )
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
        mock_client_instance.responses.create.side_effect = mock_openai.NotFoundError(
            "model not found"
        )
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


class TestAiderAdapter:
    def test_is_agent_adapter(self) -> None:
        adapter = AiderAdapter()
        assert isinstance(adapter, AgentAdapter)

    def test_name(self) -> None:
        assert AiderAdapter().name == "aider"

    def test_preflight_binary_missing(self) -> None:
        adapter = AiderAdapter()
        config = AgentConfig()
        with patch.object(adapter, "find_binary", return_value=None):
            issues = adapter.preflight(config)
        assert any("aider" in i.lower() or "pip install" in i.lower() for i in issues)

    def test_preflight_binary_found(self) -> None:
        adapter = AiderAdapter()
        config = AgentConfig()
        with patch.object(adapter, "find_binary", return_value="/usr/bin/aider"):
            issues = adapter.preflight(config)
        assert issues == []

    def test_build_command_basic(self) -> None:
        adapter = AiderAdapter()
        config = AgentConfig()
        with patch.object(adapter, "find_binary", return_value="/usr/bin/aider"):
            cmd = adapter.build_command("fix the bug", config)
        assert cmd[0] == "/usr/bin/aider"
        assert "--message" in cmd
        assert "fix the bug" in cmd
        assert "--yes-always" in cmd
        assert "--no-git" in cmd

    def test_build_command_with_model(self) -> None:
        adapter = AiderAdapter()
        config = AgentConfig(model="gpt-4o")
        with patch.object(adapter, "find_binary", return_value="/usr/bin/aider"):
            cmd = adapter.build_command("test", config)
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "gpt-4o"

    def test_build_command_without_model(self) -> None:
        adapter = AiderAdapter()
        config = AgentConfig()
        with patch.object(adapter, "find_binary", return_value="/usr/bin/aider"):
            cmd = adapter.build_command("test", config)
        assert "--model" not in cmd


class TestAiderParseOutput:
    """Tests for AiderAdapter.parse_output() — cost/token extraction from output."""

    @staticmethod
    def _load_fixture(name: str) -> str:
        return (FIXTURE_DIR / name).read_text()

    def _make_aider_result(
        self, stdout: str = "", stderr: str = "", returncode: int = 0
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["aider", "--message", "test"],
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def test_normal_cost_parsing(self) -> None:
        """Parse cost and tokens from normal Aider output."""
        adapter = AiderAdapter()
        fixture = self._load_fixture("aider_normal.txt")
        result = self._make_aider_result(stdout=fixture)
        output = adapter.parse_output(result, duration=2.5)

        assert output.input_tokens == 1200
        assert output.output_tokens == 856
        assert output.cost_usd == pytest.approx(0.0034)
        assert output.cost_model == "per_token"
        assert output.cost_source == "log_parsed"
        assert output.error is None

    def test_large_token_counts(self) -> None:
        """Parse k-suffixed token counts like 45.3k."""
        adapter = AiderAdapter()
        fixture = self._load_fixture("aider_large_tokens.txt")
        result = self._make_aider_result(stdout=fixture)
        output = adapter.parse_output(result, duration=5.0)

        assert output.input_tokens == 45300
        assert output.output_tokens == 12800
        assert output.cost_usd == pytest.approx(0.1523)
        assert output.cost_model == "per_token"
        assert output.cost_source == "log_parsed"

    def test_no_cost_line(self) -> None:
        """When no cost summary is present, tokens and cost are None."""
        adapter = AiderAdapter()
        fixture = self._load_fixture("aider_no_cost.txt")
        result = self._make_aider_result(stdout=fixture)
        output = adapter.parse_output(result, duration=1.0)

        assert output.input_tokens is None
        assert output.output_tokens is None
        assert output.cost_usd is None
        assert output.cost_source == "unavailable"
        assert output.error is None

    def test_error_output(self) -> None:
        """Aider error output still produces valid AgentOutput."""
        adapter = AiderAdapter()
        fixture = self._load_fixture("aider_error.txt")
        result = self._make_aider_result(stdout=fixture, returncode=1)
        output = adapter.parse_output(result, duration=0.5)

        assert output.exit_code == 1
        assert output.input_tokens is None
        assert output.cost_usd is None

    def test_empty_output(self) -> None:
        """Empty output produces valid AgentOutput with no tokens."""
        adapter = AiderAdapter()
        fixture = self._load_fixture("aider_empty.txt")
        result = self._make_aider_result(stdout=fixture)
        output = adapter.parse_output(result, duration=0.1)

        assert output.input_tokens is None
        assert output.output_tokens is None
        assert output.cost_usd is None

    def test_cost_in_stderr(self) -> None:
        """Cost summary found in stderr is also parsed."""
        adapter = AiderAdapter()
        result = self._make_aider_result(
            stdout="Fixed the bug",
            stderr="Tokens: 500 sent, 200 received. Cost: $0.0012 message, $0.0012 session.",
        )
        output = adapter.parse_output(result, duration=1.0)

        assert output.input_tokens == 500
        assert output.output_tokens == 200
        assert output.cost_usd == pytest.approx(0.0012)
        assert output.cost_source == "log_parsed"

    def test_session_cost_used_for_cost_usd(self) -> None:
        """The message cost (not session cost) is used for cost_usd."""
        adapter = AiderAdapter()
        result = self._make_aider_result(
            stdout="",
            stderr="Tokens: 1k sent, 500 received. Cost: $0.005 message, $0.050 session.",
        )
        output = adapter.parse_output(result, duration=1.0)

        assert output.cost_usd == pytest.approx(0.005)


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
    def test_claude_isolate_session_returns_config_dir(self) -> None:
        adapter = ClaudeAdapter()
        env = adapter.isolate_session(0)
        assert "CLAUDE_CONFIG_DIR" in env
        assert "slot-0" in env["CLAUDE_CONFIG_DIR"]

    def test_claude_isolate_session_different_slots(self) -> None:
        adapter = ClaudeAdapter()
        env0 = adapter.isolate_session(0)
        env1 = adapter.isolate_session(1)
        assert env0["CLAUDE_CONFIG_DIR"] != env1["CLAUDE_CONFIG_DIR"]

    def test_base_adapter_isolate_session_returns_empty(self) -> None:
        adapter = _StubAdapter()
        env = adapter.isolate_session(42)
        assert env == {}

    def test_copilot_isolate_session_returns_empty(self) -> None:
        adapter = CopilotAdapter()
        env = adapter.isolate_session(0)
        assert env == {}

    def test_aider_isolate_session_returns_empty(self) -> None:
        adapter = AiderAdapter()
        env = adapter.isolate_session(0)
        assert env == {}
