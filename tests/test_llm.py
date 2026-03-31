"""Tests for codeprobe.core.llm — Claude CLI utility for internal judgment."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from codeprobe.core.llm import (
    LLMExecutionError,
    LLMParseError,
    LLMRequest,
    LLMResponse,
    LLMUnavailableError,
    _parse_envelope,
    call_claude,
    claude_available,
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
        with pytest.raises(LLMParseError, match="Missing required envelope key.*result"):
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
# claude_available tests
# ---------------------------------------------------------------------------


class TestClaudeAvailable:
    def test_found(self) -> None:
        with patch("codeprobe.core.llm.shutil.which", return_value="/usr/bin/claude"):
            assert claude_available() is True

    def test_not_found(self) -> None:
        with patch("codeprobe.core.llm.shutil.which", return_value=None):
            assert claude_available() is False


# ---------------------------------------------------------------------------
# call_claude tests
# ---------------------------------------------------------------------------


class TestCallClaudeBinaryNotFound:
    def test_raises_unavailable(self) -> None:
        with patch("codeprobe.core.llm.shutil.which", return_value=None):
            request = LLMRequest(prompt="hello")
            with pytest.raises(LLMUnavailableError, match="not found"):
                call_claude(request)


class TestCallClaudeTimeout:
    def test_raises_execution_error(self) -> None:
        with (
            patch("codeprobe.core.llm.shutil.which", return_value="/usr/bin/claude"),
            patch(
                "codeprobe.core.llm.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=30),
            ),
        ):
            request = LLMRequest(prompt="hello", timeout_seconds=30)
            with pytest.raises(LLMExecutionError, match="timed out"):
                call_claude(request)


class TestCallClaudeNonZeroExit:
    def test_raises_execution_error(self) -> None:
        fake_result = subprocess.CompletedProcess(
            args=["claude"], returncode=1, stdout="", stderr="fatal error"
        )
        with (
            patch("codeprobe.core.llm.shutil.which", return_value="/usr/bin/claude"),
            patch("codeprobe.core.llm.subprocess.run", return_value=fake_result),
        ):
            request = LLMRequest(prompt="hello")
            with pytest.raises(LLMExecutionError, match="exited with code 1"):
                call_claude(request)


class TestCallClaudeInvalidJson:
    def test_raises_parse_error(self) -> None:
        fake_result = subprocess.CompletedProcess(
            args=["claude"], returncode=0, stdout="not json", stderr=""
        )
        with (
            patch("codeprobe.core.llm.shutil.which", return_value="/usr/bin/claude"),
            patch("codeprobe.core.llm.subprocess.run", return_value=fake_result),
        ):
            request = LLMRequest(prompt="hello")
            with pytest.raises(LLMParseError, match="Invalid JSON"):
                call_claude(request)


class TestCallClaudeSuccess:
    def test_returns_response(self) -> None:
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
            response = call_claude(request)

            assert isinstance(response, LLMResponse)
            assert response.text == "hello"
            assert response.input_tokens == 10
            assert response.output_tokens == 58
            assert response.cost_usd == 0.047
            assert response.model == "haiku"
            assert response.duration_ms == 1745
