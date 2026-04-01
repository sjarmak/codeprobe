"""Tests for the generic OpenAI-compatible API adapter."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from codeprobe.adapters.protocol import (
    AdapterExecutionError,
    AdapterSetupError,
    AgentAdapter,
    AgentConfig,
)


def _mock_openai_module(**overrides: object) -> MagicMock:
    """Build a mock openai module with exception types."""
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


class TestOpenAICompatAdapter:
    """Core adapter behaviour."""

    def test_satisfies_agent_adapter_protocol(self) -> None:
        from codeprobe.adapters.openai_compat import OpenAICompatAdapter

        adapter = OpenAICompatAdapter(
            api_base="http://localhost:11434/v1", model="llama3"
        )
        assert isinstance(adapter, AgentAdapter)

    def test_name_is_openai(self) -> None:
        from codeprobe.adapters.openai_compat import OpenAICompatAdapter

        adapter = OpenAICompatAdapter(
            api_base="http://localhost:11434/v1", model="llama3"
        )
        assert adapter.name == "openai"

    def test_custom_name(self) -> None:
        from codeprobe.adapters.openai_compat import OpenAICompatAdapter

        adapter = OpenAICompatAdapter(
            api_base="http://localhost:11434/v1", model="llama3", adapter_name="ollama"
        )
        assert adapter.name == "ollama"


class TestPreflight:
    """Preflight validation."""

    def test_passes_with_sdk_and_key(self) -> None:
        from codeprobe.adapters.openai_compat import OpenAICompatAdapter

        adapter = OpenAICompatAdapter(
            api_base="http://localhost:11434/v1", model="llama3"
        )
        config = AgentConfig()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            with patch.dict("sys.modules", {"openai": MagicMock()}):
                issues = adapter.preflight(config)
        assert issues == []

    def test_missing_api_key_warns(self) -> None:
        from codeprobe.adapters.openai_compat import OpenAICompatAdapter

        adapter = OpenAICompatAdapter(
            api_base="http://localhost:11434/v1", model="llama3"
        )
        config = AgentConfig()
        with patch.dict("os.environ", {}, clear=True):
            with patch.dict("sys.modules", {"openai": MagicMock()}):
                issues = adapter.preflight(config)
        assert any("OPENAI_API_KEY" in i for i in issues)

    def test_custom_api_key_env(self) -> None:
        from codeprobe.adapters.openai_compat import OpenAICompatAdapter

        adapter = OpenAICompatAdapter(
            api_base="http://localhost:11434/v1",
            model="llama3",
            api_key_env="TOGETHER_API_KEY",
        )
        config = AgentConfig()
        with patch.dict("os.environ", {"TOGETHER_API_KEY": "tok-test"}):
            with patch.dict("sys.modules", {"openai": MagicMock()}):
                issues = adapter.preflight(config)
        assert issues == []

    def test_custom_api_key_env_missing(self) -> None:
        from codeprobe.adapters.openai_compat import OpenAICompatAdapter

        adapter = OpenAICompatAdapter(
            api_base="http://localhost:11434/v1",
            model="llama3",
            api_key_env="TOGETHER_API_KEY",
        )
        config = AgentConfig()
        with patch.dict("os.environ", {}, clear=True):
            with patch.dict("sys.modules", {"openai": MagicMock()}):
                issues = adapter.preflight(config)
        assert any("TOGETHER_API_KEY" in i for i in issues)

    def test_openai_sdk_not_installed(self) -> None:
        from codeprobe.adapters.openai_compat import OpenAICompatAdapter

        adapter = OpenAICompatAdapter(
            api_base="http://localhost:11434/v1", model="llama3"
        )
        config = AgentConfig()
        with patch.dict("sys.modules", {"openai": None}):
            issues = adapter.preflight(config)
        assert any("openai" in i.lower() for i in issues)


class TestCustomBaseURL:
    """Verifies that custom base_url is passed to the OpenAI client."""

    def test_base_url_forwarded_to_client(self) -> None:
        from codeprobe.adapters.openai_compat import OpenAICompatAdapter

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 100
        mock_usage.completion_tokens = 50

        mock_message = MagicMock()
        mock_message.content = "hello"

        mock_choice = MagicMock()
        mock_choice.message = mock_message

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = mock_usage

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response

        mock_openai = _mock_openai_module()
        mock_openai.OpenAI.return_value = mock_client

        with patch.dict("sys.modules", {"openai": mock_openai}):
            adapter = OpenAICompatAdapter(
                api_base="http://localhost:11434/v1", model="llama3"
            )
            with patch.dict("os.environ", {"OPENAI_API_KEY": "test"}):
                adapter.run("hello", AgentConfig())

        mock_openai.OpenAI.assert_called_once_with(
            base_url="http://localhost:11434/v1",
            api_key="test",
        )


class TestRunSuccess:
    """Successful run scenarios."""

    def test_basic_run(self) -> None:
        from codeprobe.adapters.openai_compat import OpenAICompatAdapter

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 500
        mock_usage.completion_tokens = 200

        mock_message = MagicMock()
        mock_message.content = "Fixed the bug"

        mock_choice = MagicMock()
        mock_choice.message = mock_message

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = mock_usage

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        mock_openai = _mock_openai_module(client=mock_client)

        with patch.dict("sys.modules", {"openai": mock_openai}):
            with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
                adapter = OpenAICompatAdapter(
                    api_base="http://localhost:11434/v1", model="llama3"
                )
                output = adapter.run("fix the bug", AgentConfig())

        assert output.stdout == "Fixed the bug"
        assert output.input_tokens == 500
        assert output.output_tokens == 200
        assert output.exit_code == 0
        assert output.duration_seconds > 0

    def test_model_override_from_config(self) -> None:
        """AgentConfig.model overrides the adapter's default model."""
        from codeprobe.adapters.openai_compat import OpenAICompatAdapter

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 100
        mock_usage.completion_tokens = 50

        mock_message = MagicMock()
        mock_message.content = "ok"

        mock_choice = MagicMock()
        mock_choice.message = mock_message

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = mock_usage

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        mock_openai = _mock_openai_module(client=mock_client)

        with patch.dict("sys.modules", {"openai": mock_openai}):
            with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
                adapter = OpenAICompatAdapter(
                    api_base="http://together.xyz/v1", model="llama3"
                )
                adapter.run("test", AgentConfig(model="mixtral-8x7b"))

        call_kwargs = mock_client.chat.completions.create.call_args
        assert call_kwargs.kwargs["model"] == "mixtral-8x7b"

    def test_no_usage_returns_error_field(self) -> None:
        from codeprobe.adapters.openai_compat import OpenAICompatAdapter

        mock_message = MagicMock()
        mock_message.content = "response"

        mock_choice = MagicMock()
        mock_choice.message = mock_message

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = None

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        mock_openai = _mock_openai_module(client=mock_client)

        with patch.dict("sys.modules", {"openai": mock_openai}):
            with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
                adapter = OpenAICompatAdapter(
                    api_base="http://localhost:11434/v1", model="llama3"
                )
                output = adapter.run("test", AgentConfig())

        assert output.stdout == "response"
        assert output.error is not None
        assert "usage" in output.error.lower()
        assert output.cost_source == "unavailable"

    def test_empty_choices_returns_empty_stdout(self) -> None:
        from codeprobe.adapters.openai_compat import OpenAICompatAdapter

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 10
        mock_usage.completion_tokens = 0

        mock_response = MagicMock()
        mock_response.choices = []
        mock_response.usage = mock_usage

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        mock_openai = _mock_openai_module(client=mock_client)

        with patch.dict("sys.modules", {"openai": mock_openai}):
            with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
                adapter = OpenAICompatAdapter(
                    api_base="http://localhost:11434/v1", model="llama3"
                )
                output = adapter.run("test", AgentConfig())

        assert output.stdout == ""


class TestRunErrors:
    """Error handling during run."""

    def test_auth_error_raises_setup_error(self) -> None:
        from codeprobe.adapters.openai_compat import OpenAICompatAdapter

        mock_client = MagicMock()
        mock_openai = _mock_openai_module(client=mock_client)
        mock_client.chat.completions.create.side_effect = (
            mock_openai.AuthenticationError("bad key")
        )

        with patch.dict("sys.modules", {"openai": mock_openai}):
            with patch.dict("os.environ", {"OPENAI_API_KEY": "bad"}):
                adapter = OpenAICompatAdapter(
                    api_base="http://localhost:11434/v1", model="llama3"
                )
                with pytest.raises(AdapterSetupError, match="API key"):
                    adapter.run("test", AgentConfig())

    def test_rate_limit_raises_execution_error(self) -> None:
        from codeprobe.adapters.openai_compat import OpenAICompatAdapter

        mock_client = MagicMock()
        mock_openai = _mock_openai_module(client=mock_client)
        mock_client.chat.completions.create.side_effect = mock_openai.RateLimitError(
            "too many"
        )

        with patch.dict("sys.modules", {"openai": mock_openai}):
            with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
                adapter = OpenAICompatAdapter(
                    api_base="http://localhost:11434/v1", model="llama3"
                )
                with pytest.raises(AdapterExecutionError, match="Rate limited"):
                    adapter.run("test", AgentConfig())

    def test_api_error_raises_execution_error(self) -> None:
        from codeprobe.adapters.openai_compat import OpenAICompatAdapter

        mock_client = MagicMock()
        mock_openai = _mock_openai_module(client=mock_client)
        mock_client.chat.completions.create.side_effect = mock_openai.APIError(
            "server down"
        )

        with patch.dict("sys.modules", {"openai": mock_openai}):
            with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
                adapter = OpenAICompatAdapter(
                    api_base="http://localhost:11434/v1", model="llama3"
                )
                with pytest.raises(AdapterExecutionError, match="API error"):
                    adapter.run("test", AgentConfig())

    def test_openai_not_installed_raises_setup_error(self) -> None:
        from codeprobe.adapters.openai_compat import OpenAICompatAdapter

        adapter = OpenAICompatAdapter(
            api_base="http://localhost:11434/v1", model="llama3"
        )
        with patch.dict("sys.modules", {"openai": None}):
            with pytest.raises(AdapterSetupError, match="openai"):
                adapter.run("test", AgentConfig())


class TestUsageExtraction:
    """Token/cost extraction from API responses."""

    def test_cost_unavailable_for_unknown_model(self) -> None:
        """Models not in pricing table get cost_source='unavailable'."""
        from codeprobe.adapters.openai_compat import OpenAICompatAdapter

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 1000
        mock_usage.completion_tokens = 500

        mock_message = MagicMock()
        mock_message.content = "ok"

        mock_choice = MagicMock()
        mock_choice.message = mock_message

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = mock_usage

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        mock_openai = _mock_openai_module(client=mock_client)

        with patch.dict("sys.modules", {"openai": mock_openai}):
            with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
                # No pricing for arbitrary models
                adapter = OpenAICompatAdapter(
                    api_base="http://localhost:11434/v1", model="llama3"
                )
                output = adapter.run("test", AgentConfig())

        assert output.input_tokens == 1000
        assert output.output_tokens == 500
        assert output.cost_usd is None
        assert output.cost_model == "unknown"
        assert output.cost_source == "unavailable"

    def test_custom_pricing(self) -> None:
        """Custom pricing table allows cost calculation."""
        from codeprobe.adapters.openai_compat import OpenAICompatAdapter

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 1000
        mock_usage.completion_tokens = 500

        mock_message = MagicMock()
        mock_message.content = "ok"

        mock_choice = MagicMock()
        mock_choice.message = mock_message

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = mock_usage

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response
        mock_openai = _mock_openai_module(client=mock_client)

        pricing = {"llama3": (0.50, 1.00)}

        with patch.dict("sys.modules", {"openai": mock_openai}):
            with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
                adapter = OpenAICompatAdapter(
                    api_base="http://localhost:11434/v1",
                    model="llama3",
                    pricing=pricing,
                )
                output = adapter.run("test", AgentConfig())

        expected_cost = 1000 * 0.50 / 1_000_000 + 500 * 1.00 / 1_000_000
        assert output.cost_usd == pytest.approx(expected_cost)
        assert output.cost_model == "per_token"
        assert output.cost_source == "calculated"
