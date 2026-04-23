"""Cross-backend defaults — max_tokens fall-through, placeholder guards.

These tests run fully offline: we patch ``sys.modules`` to inject fake
vendor SDKs and assert the backend shim forwards the expected kwargs.
No network, no credentials required.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from codeprobe.llm import load_registry
from codeprobe.llm.backends import (
    AnthropicBackend,
    AzureOpenAIBackend,
    BedrockBackend,
    OpenAICompatBackend,
    VertexBackend,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _registry_with_valid_bedrock_arn(tmp_path: Any) -> Any:
    """A registry with the Bedrock placeholder substituted — the shipped
    one refuses to resolve (see test_registry.py). We need a usable one
    for exercising BedrockBackend.complete().
    """
    yaml_text = (
        "models:\n"
        "  opus-4.7:\n"
        "    anthropic: claude-opus-4-7\n"
        "    bedrock: arn:aws:bedrock:us-east-1:123456789012:inference-profile/x\n"
        "    vertex: publishers/anthropic/models/claude-opus-4-7\n"
        "    azure_openai:\n"
        "      deployment: opus-4-7-prod\n"
        "      api_version: '2024-10-21'\n"
        "    openai_compat: claude-opus-4-7\n"
    )
    path = tmp_path / "registry.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return load_registry(path)


def _fake_openai_chat_response() -> MagicMock:
    mock_choice = MagicMock()
    mock_choice.message.content = "hello"
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage.prompt_tokens = 5
    mock_response.usage.completion_tokens = 7
    return mock_response


def _fake_anthropic_message() -> MagicMock:
    msg = MagicMock()
    msg.content = [MagicMock(text="hello")]
    msg.usage.input_tokens = 3
    msg.usage.output_tokens = 4
    return msg


# ---------------------------------------------------------------------------
# max_tokens default coverage
# ---------------------------------------------------------------------------


class TestAnthropicBackendDefaults:
    def test_complete_without_max_tokens_uses_default_1024(self) -> None:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _fake_anthropic_message()
        mock_anthropic = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}),
            patch.dict("sys.modules", {"anthropic": mock_anthropic}),
        ):
            backend = AnthropicBackend()
            backend.complete(
                messages=[{"role": "user", "content": "hi"}],
                model_id="claude-opus-4-7",
            )

        _, kwargs = mock_client.messages.create.call_args
        assert kwargs["max_tokens"] == 1024


class TestBedrockBackendDefaults:
    def test_complete_without_max_tokens_uses_default_1024(
        self, tmp_path: Any
    ) -> None:
        mock_boto3 = MagicMock()
        mock_client = MagicMock()
        mock_body = MagicMock()
        mock_body.read.return_value = (
            b'{"content": [{"type": "text", "text": "hello"}],'
            b' "usage": {"input_tokens": 1, "output_tokens": 2}}'
        )
        mock_client.invoke_model.return_value = {"body": mock_body}
        mock_boto3.client.return_value = mock_client

        registry = _registry_with_valid_bedrock_arn(tmp_path)
        backend = BedrockBackend(registry=registry)
        model_id = backend.resolve_model_id("opus-4.7")

        with (
            patch.dict("os.environ", {"AWS_ACCESS_KEY_ID": "k"}),
            patch.dict("sys.modules", {"boto3": mock_boto3}),
        ):
            backend.complete(
                messages=[{"role": "user", "content": "hi"}],
                model_id=model_id,
            )

        import json as _json

        _, call_kwargs = mock_client.invoke_model.call_args
        body = _json.loads(call_kwargs["body"])
        assert body["max_tokens"] == 1024


class TestAzureOpenAIBackendDefaults:
    def test_complete_without_max_tokens_uses_default_1024(self) -> None:
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = (
            _fake_openai_chat_response()
        )
        mock_openai = MagicMock()
        mock_openai.AzureOpenAI.return_value = mock_client

        with (
            patch.dict(
                "os.environ",
                {
                    "AZURE_OPENAI_ENDPOINT": "https://x.openai.azure.com/",
                    "AZURE_OPENAI_API_KEY": "sk-test",
                },
            ),
            patch.dict("sys.modules", {"openai": mock_openai}),
        ):
            backend = AzureOpenAIBackend()
            backend.complete(
                messages=[{"role": "user", "content": "hi"}],
                model_id={"deployment": "dep", "api_version": "2024-10-21"},
            )

        _, kwargs = mock_client.chat.completions.create.call_args
        assert kwargs["max_tokens"] == 1024

    def test_complete_with_explicit_max_tokens_overrides_default(self) -> None:
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = (
            _fake_openai_chat_response()
        )
        mock_openai = MagicMock()
        mock_openai.AzureOpenAI.return_value = mock_client

        with (
            patch.dict(
                "os.environ",
                {
                    "AZURE_OPENAI_ENDPOINT": "https://x.openai.azure.com/",
                    "AZURE_OPENAI_API_KEY": "sk-test",
                },
            ),
            patch.dict("sys.modules", {"openai": mock_openai}),
        ):
            AzureOpenAIBackend().complete(
                messages=[{"role": "user", "content": "hi"}],
                model_id={"deployment": "dep", "api_version": "2024-10-21"},
                max_tokens=256,
            )

        _, kwargs = mock_client.chat.completions.create.call_args
        assert kwargs["max_tokens"] == 256


class TestOpenAICompatBackendDefaults:
    def test_complete_without_max_tokens_uses_default_1024(self) -> None:
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = (
            _fake_openai_chat_response()
        )
        mock_openai = MagicMock()
        mock_openai.OpenAI.return_value = mock_client

        with (
            patch.dict(
                "os.environ",
                {
                    "OPENAI_COMPAT_BASE_URL": "https://gw.example.com/v1",
                    "OPENAI_COMPAT_API_KEY": "sk-test",
                },
            ),
            patch.dict("sys.modules", {"openai": mock_openai}),
        ):
            backend = OpenAICompatBackend()
            backend.complete(
                messages=[{"role": "user", "content": "hi"}],
                model_id="claude-opus-4-7",
            )

        _, kwargs = mock_client.chat.completions.create.call_args
        assert kwargs["max_tokens"] == 1024


class TestVertexBackendDefaults:
    def test_complete_without_max_tokens_uses_default_1024(self) -> None:
        fake_message = _fake_anthropic_message()
        mock_client = MagicMock()
        mock_client.messages.create.return_value = fake_message
        mock_anthropic_mod = MagicMock()
        mock_anthropic_mod.AnthropicVertex.return_value = mock_client

        with (
            patch.dict(
                "os.environ",
                {
                    "GOOGLE_CLOUD_PROJECT": "p",
                    "GOOGLE_CLOUD_REGION": "us-east5",
                },
            ),
            patch.dict("sys.modules", {"anthropic": mock_anthropic_mod}),
        ):
            VertexBackend().complete(
                messages=[{"role": "user", "content": "hi"}],
                model_id="publishers/anthropic/models/claude-opus-4-7",
            )

        _, kwargs = mock_client.messages.create.call_args
        assert kwargs["max_tokens"] == 1024


# ---------------------------------------------------------------------------
# Cross-backend parametrized smoke: all backends accept default kwargs.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "backend_cls",
    [AnthropicBackend, AzureOpenAIBackend, OpenAICompatBackend, VertexBackend],
)
def test_all_openai_anthropic_backends_have_max_tokens_default(
    backend_cls: type,
) -> None:
    """Structural invariant: every non-Bedrock backend's complete() path
    pops ``max_tokens`` with a 1024 default. We verify by reading the
    source so the test flags any future drift.
    """
    import inspect

    src = inspect.getsource(backend_cls.complete)
    assert "max_tokens" in src, (
        f"{backend_cls.__name__}.complete() must handle max_tokens"
    )
    assert "1024" in src, (
        f"{backend_cls.__name__}.complete() must default max_tokens to 1024"
    )
