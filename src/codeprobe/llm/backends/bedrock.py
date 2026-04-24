"""Backend shim for Amazon Bedrock (Anthropic models via AWS).

Lazily imports ``boto3`` and the ``anthropic`` SDK's ``AnthropicBedrock``
client. ``boto3`` is intentionally NOT a required dep of codeprobe — the
import happens inside :meth:`complete` so that test collection and
import of this module succeed without AWS libs installed.
"""

from __future__ import annotations

import json
import os
from typing import Any

from codeprobe.llm import ModelRegistry, get_registry
from codeprobe.llm.backends.base import (
    BackendExecutionError,
    BackendUnavailableError,
)

__all__ = ["BedrockBackend"]


# Sentinels that indicate an un-customized registry entry. If present in a
# resolved ARN we raise loudly rather than letting boto3 fail deep inside
# an AWS call with a much less actionable error.
_PLACEHOLDER_ACCOUNT_SENTINELS: tuple[str, ...] = (
    "REPLACE_WITH_YOUR_AWS_ACCOUNT_ID",
    ":000000000000:",
)


class BedrockBackend:
    """Thin adapter over ``boto3.client('bedrock-runtime')``."""

    name: str = "bedrock"

    # Env keys that, when all absent, indicate unconfigured creds.
    CRED_ENV_VARS: tuple[str, ...] = (
        "AWS_ACCESS_KEY_ID",
        "AWS_PROFILE",
        "AWS_SESSION_TOKEN",
    )

    def __init__(self, registry: ModelRegistry | None = None) -> None:
        self._registry = registry or get_registry()

    def resolve_model_id(self, logical_name: str) -> str:
        value = self._registry.resolve(logical_name, self.name)
        if not isinstance(value, str) or not value:
            raise BackendExecutionError(
                f"Registry entry for bedrock/{logical_name} must be a "
                f"non-empty ARN string, got {type(value).__name__}"
            )
        for sentinel in _PLACEHOLDER_ACCOUNT_SENTINELS:
            if sentinel in value:
                raise BackendExecutionError(
                    f"Bedrock ARN for {logical_name!r} still contains "
                    f"placeholder account ID ({sentinel!r}). Edit "
                    "src/codeprobe/llm/model_registry.yaml and replace "
                    "REPLACE_WITH_YOUR_AWS_ACCOUNT_ID with your 12-digit "
                    "AWS account number before calling the Bedrock backend."
                )
        return value

    def complete(
        self,
        messages: list[dict[str, Any]],
        model_id: str | dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not isinstance(model_id, str):
            raise BackendExecutionError(
                "BedrockBackend.complete requires a string model_id (ARN)"
            )

        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:
            raise BackendUnavailableError(
                "boto3 not installed (pip install boto3) — required for Bedrock"
            ) from exc

        if not any(os.environ.get(k) for k in self.CRED_ENV_VARS):
            raise BackendUnavailableError(
                "AWS credentials not configured "
                f"(none of {', '.join(self.CRED_ENV_VARS)} set)"
            )

        region = os.environ.get("AWS_REGION", "us-east-1")
        client = boto3.client("bedrock-runtime", region_name=region)

        max_tokens = int(kwargs.pop("max_tokens", 1024))
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "messages": messages,
            **kwargs,
        }
        try:
            response = client.invoke_model(
                modelId=model_id,
                body=json.dumps(body),
                contentType="application/json",
                accept="application/json",
            )
            payload = json.loads(response["body"].read())
        except Exception as exc:  # pragma: no cover - network path
            raise BackendExecutionError(f"Bedrock API error: {exc}") from exc

        text = ""
        for block in payload.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                text += str(block.get("text", ""))

        usage = payload.get("usage") or {}
        return {
            "text": text,
            "model": model_id,
            "backend": self.name,
            "usage": {
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
            },
        }
