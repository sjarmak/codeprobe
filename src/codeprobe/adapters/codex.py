"""OpenAI Codex agent adapter — API-based (no CLI subprocess)."""

from __future__ import annotations

import logging
import os
import time

from codeprobe.adapters.protocol import (
    AdapterExecutionError,
    AdapterSetupError,
    AgentConfig,
    AgentOutput,
)
from codeprobe.adapters.telemetry import ApiResponseCollector

logger = logging.getLogger(__name__)


def _usage_fields(
    usage: object | None, input_attr: str, output_attr: str
) -> tuple[int | None, int | None]:
    """Extract input/output token counts from an API usage object."""
    if usage is None:
        return None, None
    return getattr(usage, input_attr, None), getattr(usage, output_attr, None)


class CodexAdapter:
    """Adapter for OpenAI Codex API.

    Tries the Responses API (responses.create) first. If the model is not
    available on that endpoint (NotFoundError), falls back to the Chat
    Completions API (chat.completions.create).
    """

    def __init__(self) -> None:
        self._collector = ApiResponseCollector()

    @property
    def name(self) -> str:
        return "codex"

    def preflight(self, config: AgentConfig) -> list[str]:
        issues: list[str] = []
        try:
            import openai  # noqa: F401
        except ImportError:
            issues.append(
                "openai SDK not found. Install with: pip install codeprobe[codex]"
            )
            return issues
        if not os.environ.get("OPENAI_API_KEY"):
            issues.append("OPENAI_API_KEY environment variable not set")
        return issues

    def isolate_session(self, slot_id: int) -> dict[str, str]:
        """Codex uses API calls — no session-level isolation needed."""
        return {}

    def run(
        self,
        prompt: str,
        config: AgentConfig,
        session_env: dict[str, str] | None = None,
    ) -> AgentOutput:
        try:
            import openai
        except ImportError:
            raise AdapterSetupError(
                "openai SDK not installed. Run: pip install codeprobe[codex]"
            )

        client = openai.OpenAI()
        model = config.model or "codex-mini-latest"
        start = time.monotonic()

        try:
            try:
                response = client.responses.create(model=model, input=prompt)
                stdout = response.output_text or ""
                input_tokens, output_tokens = _usage_fields(
                    response.usage, "input_tokens", "output_tokens"
                )
            except openai.NotFoundError:
                logger.info(
                    "Model %s not found on Responses API, falling back to "
                    "Chat Completions API",
                    model,
                )
                try:
                    chat_response = client.chat.completions.create(
                        model=model,
                        messages=[{"role": "user", "content": prompt}],
                    )
                except openai.NotFoundError as exc:
                    raise AdapterExecutionError(
                        f"Model {model!r} not available on Responses or "
                        f"Chat Completions API: {exc}"
                    ) from exc
                content = (
                    chat_response.choices[0].message.content
                    if chat_response.choices
                    else None
                )
                stdout = content or ""
                input_tokens, output_tokens = _usage_fields(
                    chat_response.usage, "prompt_tokens", "completion_tokens"
                )
        except openai.AuthenticationError as exc:
            raise AdapterSetupError(f"OPENAI_API_KEY invalid: {exc}") from exc
        except openai.RateLimitError as exc:
            raise AdapterExecutionError(f"Rate limited: {exc}") from exc
        except openai.APIError as exc:
            raise AdapterExecutionError(f"OpenAI API error: {exc}") from exc

        duration = time.monotonic() - start

        usage = self._collector.collect(
            stdout,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
        )

        return AgentOutput(
            stdout=stdout,
            stderr=None,
            exit_code=0,
            duration_seconds=duration,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd,
            cost_model=usage.cost_model,
            cost_source=usage.cost_source,
            error=usage.error,
        )
