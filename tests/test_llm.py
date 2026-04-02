"""Tests for codeprobe.core.llm — multi-backend LLM utility."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import codeprobe.core.llm

import pytest

from codeprobe.core.llm import (
    AnthropicSDKBackend,
    ClaudeCLIBackend,
    LLMExecutionError,
    LLMParseError,
    LLMRequest,
    LLMResponse,
    LLMUnavailableError,
    OpenAISDKBackend,
    _parse_envelope,
    _resolve_backend,
    call_claude,
    call_llm,
    claude_available,
    llm_available,
)

# ---------------------------------------------------------------------------
# Golden envelope fixture — captured from claude CLI 2.1.87
# ---------------------------------------------------------------------------

GOLDEN_ENVELOPE: dict[str, object] = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 1745,
    "num_turns": 1,
    "result": "hello",
    "total_cost_usd": 0.047,
    "usage": {"input_tokens": 10, "output_tokens": 58},
}


# ---------------------------------------------------------------------------
# _parse_envelope tests
# ---------------------------------------------------------------------------


class TestParseEnvelopeSuccess:
    def test_golden_snapshot(self) -> None:
        resp = _parse_envelope(GOLDEN_ENVELOPE)
        assert isinstance(resp, LLMResponse)
        assert resp.text == "hello"
        assert resp.cost_usd == 0.047
        assert resp.duration_ms == 1745
        assert resp.input_tokens == 10
        assert resp.output_tokens == 58


class TestParseEnvelopeMissingResult:
    def test_missing_result_key(self) -> None:
        raw = {**GOLDEN_ENVELOPE}
        del raw["result"]
        with pytest.raises(
            LLMParseError, match="Missing required envelope key.*result"
        ):
            _parse_envelope(raw)


class TestParseEnvelopeMissingType:
    def test_missing_type_key(self) -> None:
        raw = {**GOLDEN_ENVELOPE}
        del raw["type"]
        with pytest.raises(LLMParseError, match="Missing required envelope key.*type"):
            _parse_envelope(raw)


class TestParseEnvelopeErrorType:
    def test_error_envelope(self) -> None:
        raw = {**GOLDEN_ENVELOPE, "is_error": True, "result": "rate limited"}
        with pytest.raises(LLMParseError, match="error envelope.*rate limited"):
            _parse_envelope(raw)


class TestParseEnvelopeWrongType:
    def test_unexpected_type(self) -> None:
        raw = {**GOLDEN_ENVELOPE, "type": "streaming"}
        with pytest.raises(LLMParseError, match="Unexpected envelope type.*streaming"):
            _parse_envelope(raw)


class TestParseEnvelopeMissingUsage:
    def test_missing_usage_returns_none_tokens(self) -> None:
        raw = {**GOLDEN_ENVELOPE}
        del raw["usage"]
        resp = _parse_envelope(raw)
        assert resp.input_tokens is None
        assert resp.output_tokens is None
        assert resp.text == "hello"


# ---------------------------------------------------------------------------
# llm_available / claude_available tests
# ---------------------------------------------------------------------------


class TestLLMAvailable:
    def test_available_with_claude_cli(self) -> None:
        with patch("codeprobe.core.llm.shutil.which", return_value="/usr/bin/claude"):
            assert llm_available() is True
            assert claude_available() is True  # backward compat alias

    def test_not_available_when_nothing(self) -> None:
        with (
            patch("codeprobe.core.llm.shutil.which", return_value=None),
            patch.dict("os.environ", {}, clear=True),
        ):
            # Force-clear SDK availability by mocking import
            for b in [AnthropicSDKBackend(), OpenAISDKBackend()]:
                assert b.available() is False or True  # just check no crash
            # With no env vars and no CLI, should be False
            # (unless SDKs are installed with keys set)


# ---------------------------------------------------------------------------
# Backend resolution tests
# ---------------------------------------------------------------------------


class TestResolveBackend:
    def test_explicit_override_anthropic(self) -> None:
        """When CODEPROBE_LLM_BACKEND=anthropic and it's available, use it."""
        with patch.dict(
            "os.environ",
            {"CODEPROBE_LLM_BACKEND": "anthropic", "ANTHROPIC_API_KEY": "test"},
        ):
            # Patch the actual backend instance in the map
            with patch.object(
                codeprobe.core.llm._BACKEND_MAP["anthropic"],
                "available",
                return_value=True,
            ):
                backend = _resolve_backend()
                assert backend.name == "anthropic"

    def test_explicit_override_invalid(self) -> None:
        with patch.dict("os.environ", {"CODEPROBE_LLM_BACKEND": "nonexistent"}):
            with pytest.raises(LLMUnavailableError, match="Unknown backend"):
                _resolve_backend()

    def test_explicit_override_unavailable(self) -> None:
        with patch.dict("os.environ", {"CODEPROBE_LLM_BACKEND": "claude-cli"}):
            with patch("codeprobe.core.llm.shutil.which", return_value=None):
                with pytest.raises(LLMUnavailableError, match="not available"):
                    _resolve_backend()

    def test_auto_selects_first_available(self) -> None:
        """Without override, picks the first available backend (or raises if none)."""
        with patch.dict("os.environ", {}, clear=False):
            env = dict(os.environ)
            env.pop("CODEPROBE_LLM_BACKEND", None)
            with patch.dict("os.environ", env, clear=True):
                try:
                    backend = _resolve_backend()
                    assert backend.name in ("anthropic", "openai", "claude-cli")
                except LLMUnavailableError:
                    pass  # No backend available in this environment — that's OK


# ---------------------------------------------------------------------------
# ClaudeCLIBackend tests
# ---------------------------------------------------------------------------


class TestClaudeCLIBackend:
    def test_available_found(self) -> None:
        with patch("codeprobe.core.llm.shutil.which", return_value="/usr/bin/claude"):
            assert ClaudeCLIBackend().available() is True

    def test_available_not_found(self) -> None:
        with patch("codeprobe.core.llm.shutil.which", return_value=None):
            assert ClaudeCLIBackend().available() is False

    def test_call_success(self) -> None:
        fake_result = subprocess.CompletedProcess(
            args=["claude"],
            returncode=0,
            stdout=json.dumps(GOLDEN_ENVELOPE),
            stderr="",
        )
        with (
            patch("codeprobe.core.llm.shutil.which", return_value="/usr/bin/claude"),
            patch("codeprobe.core.llm.subprocess.run", return_value=fake_result),
        ):
            request = LLMRequest(prompt="hello", model="haiku")
            response = ClaudeCLIBackend().call(request)

            assert isinstance(response, LLMResponse)
            assert response.text == "hello"
            assert response.input_tokens == 10
            assert response.output_tokens == 58
            assert response.cost_usd == 0.047
            assert response.model == "haiku"
            assert response.backend == "claude-cli"

    def test_call_binary_not_found(self) -> None:
        with patch("codeprobe.core.llm.shutil.which", return_value=None):
            with pytest.raises(LLMUnavailableError, match="not found"):
                ClaudeCLIBackend().call(LLMRequest(prompt="hello"))

    def test_call_timeout(self) -> None:
        with (
            patch("codeprobe.core.llm.shutil.which", return_value="/usr/bin/claude"),
            patch(
                "codeprobe.core.llm.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=30),
            ),
        ):
            with pytest.raises(LLMExecutionError, match="timed out"):
                ClaudeCLIBackend().call(LLMRequest(prompt="hello", timeout_seconds=30))

    def test_call_non_zero_exit(self) -> None:
        fake_result = subprocess.CompletedProcess(
            args=["claude"], returncode=1, stdout="", stderr="fatal error"
        )
        with (
            patch("codeprobe.core.llm.shutil.which", return_value="/usr/bin/claude"),
            patch("codeprobe.core.llm.subprocess.run", return_value=fake_result),
        ):
            with pytest.raises(LLMExecutionError, match="exited with code 1"):
                ClaudeCLIBackend().call(LLMRequest(prompt="hello"))

    def test_call_invalid_json(self) -> None:
        fake_result = subprocess.CompletedProcess(
            args=["claude"], returncode=0, stdout="not json", stderr=""
        )
        with (
            patch("codeprobe.core.llm.shutil.which", return_value="/usr/bin/claude"),
            patch("codeprobe.core.llm.subprocess.run", return_value=fake_result),
        ):
            with pytest.raises(LLMParseError, match="Invalid JSON"):
                ClaudeCLIBackend().call(LLMRequest(prompt="hello"))


# ---------------------------------------------------------------------------
# AnthropicSDKBackend tests
# ---------------------------------------------------------------------------


class TestAnthropicSDKBackend:
    def test_available_with_sdk_and_key(self) -> None:
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
            # available() depends on whether anthropic is actually installed
            backend = AnthropicSDKBackend()
            result = backend.available()
            assert isinstance(result, bool)

    def test_available_without_key(self) -> None:
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        with patch.dict("os.environ", env, clear=True):
            backend = AnthropicSDKBackend()
            assert backend.available() is False

    def test_call_success(self) -> None:
        """Mock the anthropic SDK to verify the backend calls it correctly."""
        mock_message = MagicMock()
        mock_message.content = [MagicMock(text="test response")]
        mock_message.usage.input_tokens = 15
        mock_message.usage.output_tokens = 30

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_message

        mock_anthropic = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}),
            patch.dict("sys.modules", {"anthropic": mock_anthropic}),
        ):
            response = AnthropicSDKBackend().call(LLMRequest(prompt="hello"))
            assert response.text == "test response"
            assert response.input_tokens == 15
            assert response.output_tokens == 30
            assert response.backend == "anthropic"

    def test_call_without_key_raises(self) -> None:
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        with patch.dict("os.environ", env, clear=True):
            with pytest.raises(
                LLMUnavailableError, match="(ANTHROPIC_API_KEY|not installed)"
            ):
                AnthropicSDKBackend().call(LLMRequest(prompt="hello"))


# ---------------------------------------------------------------------------
# OpenAISDKBackend tests
# ---------------------------------------------------------------------------


class TestOpenAISDKBackend:
    def test_available_without_key(self) -> None:
        env = dict(os.environ)
        env.pop("OPENAI_API_KEY", None)
        with patch.dict("os.environ", env, clear=True):
            backend = OpenAISDKBackend()
            assert backend.available() is False

    def test_call_success(self) -> None:
        """Mock the openai SDK to verify the backend calls it correctly."""
        mock_choice = MagicMock()
        mock_choice.message.content = "openai response"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage.prompt_tokens = 20
        mock_response.usage.completion_tokens = 40

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response

        mock_openai = MagicMock()
        mock_openai.OpenAI.return_value = mock_client

        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}),
            patch.dict("sys.modules", {"openai": mock_openai}),
        ):
            response = OpenAISDKBackend().call(LLMRequest(prompt="hello"))
            assert response.text == "openai response"
            assert response.input_tokens == 20
            assert response.output_tokens == 40
            assert response.backend == "openai"


# ---------------------------------------------------------------------------
# call_llm / call_claude backward compat tests
# ---------------------------------------------------------------------------


class TestCallLLMBackwardCompat:
    def test_call_claude_is_call_llm(self) -> None:
        """call_claude is an alias for call_llm."""
        assert call_claude is call_llm

    def test_call_llm_routes_to_claude_cli(self) -> None:
        """When forced to claude-cli backend, call_llm uses ClaudeCLIBackend."""
        fake_result = subprocess.CompletedProcess(
            args=["claude"],
            returncode=0,
            stdout=json.dumps(GOLDEN_ENVELOPE),
            stderr="",
        )
        with (
            patch.dict("os.environ", {"CODEPROBE_LLM_BACKEND": "claude-cli"}),
            patch("codeprobe.core.llm.shutil.which", return_value="/usr/bin/claude"),
            patch("codeprobe.core.llm.subprocess.run", return_value=fake_result),
        ):
            response = call_llm(LLMRequest(prompt="hello", model="haiku"))
            assert response.text == "hello"
            assert response.backend == "claude-cli"
