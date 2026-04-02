"""Generic OpenAI-compatible API adapter.

Works with any endpoint that speaks the OpenAI Chat Completions API:
Ollama, Together, vLLM, Anyscale, Groq, etc.  Configure via constructor
args or ``AgentConfig.extra``.
"""

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


class OpenAICompatAdapter:
    """Adapter for any OpenAI-compatible Chat Completions API.

    Parameters
    ----------
    api_base:
        Base URL of the API (e.g. ``http://localhost:11434/v1``).
    model:
        Default model name.  Can be overridden per-run via ``AgentConfig.model``.
    api_key_env:
        Name of the environment variable holding the API key.
        Defaults to ``OPENAI_API_KEY``.
    adapter_name:
        Human-readable adapter name returned by ``name``.  Defaults to ``"openai"``.
    pricing:
        Optional per-model pricing table ``{model: (input_per_1M, output_per_1M)}``.
        When provided, enables per-token cost calculation.
    """

    def __init__(
        self,
        *,
        api_base: str,
        model: str,
        api_key_env: str = "OPENAI_API_KEY",
        adapter_name: str = "openai",
        pricing: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        self._api_base = api_base
        self._model = model
        self._api_key_env = api_key_env
        self._adapter_name = adapter_name
        self._collector = ApiResponseCollector(pricing=pricing or {})

    @property
    def name(self) -> str:
        return self._adapter_name

    def preflight(self, config: AgentConfig) -> list[str]:
        issues: list[str] = []
        try:
            import openai  # noqa: F401
        except ImportError:
            issues.append(
                "openai SDK not found. Install with: pip install codeprobe[codex]"
            )
            return issues
        if not os.environ.get(self._api_key_env):
            issues.append(f"{self._api_key_env} environment variable not set")
        return issues

    def isolate_session(self, slot_id: int) -> dict[str, str]:
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

        api_key = os.environ.get(self._api_key_env, "")
        client = openai.OpenAI(base_url=self._api_base, api_key=api_key)
        model = config.model or self._model
        start = time.monotonic()

        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
            )
        except openai.AuthenticationError as exc:
            raise AdapterSetupError(
                f"API key invalid ({self._api_key_env}): {exc}"
            ) from exc
        except openai.RateLimitError as exc:
            raise AdapterExecutionError(f"Rate limited: {exc}") from exc
        except openai.APIError as exc:
            raise AdapterExecutionError(f"OpenAI-compatible API error: {exc}") from exc

        duration = time.monotonic() - start

        content = response.choices[0].message.content if response.choices else None
        stdout = content or ""

        input_tokens: int | None = None
        output_tokens: int | None = None
        if response.usage is not None:
            input_tokens = getattr(response.usage, "prompt_tokens", None)
            output_tokens = getattr(response.usage, "completion_tokens", None)

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
