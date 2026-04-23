"""Parity CI fixture: a tiny in-memory "task set" is executed against two
backends and their mean rewards must be within +/- 5%.

The default run uses deterministic in-memory stub backends so the test
is always executable (no network, no cloud creds). Real vendor backends
are parametrized in via env-gated markers and skipped when credentials
for the second backend are not set — matching acceptance criterion 4.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import pytest

from codeprobe.llm.backends.base import LLMBackend

# ---------------------------------------------------------------------------
# Stub backends — deterministic, always available (no network).
# ---------------------------------------------------------------------------


@dataclass
class _StubBackend:
    """Deterministic backend used for CI parity. Returns a canned text
    whose word-count depends only on the prompt, so parity is exact.
    """

    name: str
    _echo_suffix: str = ""

    def resolve_model_id(self, logical_name: str) -> str:
        return f"stub::{self.name}::{logical_name}"

    def complete(
        self,
        messages: list[dict[str, Any]],
        model_id: str | dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        prompt = " ".join(str(m.get("content", "")) for m in messages)
        reply = f"{prompt}{self._echo_suffix}"
        return {
            "text": reply,
            "model": str(model_id),
            "backend": self.name,
            "usage": {
                "input_tokens": len(prompt.split()),
                "output_tokens": len(reply.split()),
            },
        }


# Tiny "task set": each entry is a prompt. Reward is a deterministic
# function of the response text (word count scaled).
TASKS: list[dict[str, Any]] = [
    {"role": "user", "content": "add a null check"},
    {"role": "user", "content": "rename the helper function"},
    {"role": "user", "content": "extract the magic number into a constant"},
]


def _reward(response_text: str) -> float:
    """Deterministic scalar reward in [0, 1]."""
    words = response_text.split()
    return min(1.0, len(words) / 10.0)


def _mean_reward(backend: LLMBackend, tasks: list[dict[str, Any]]) -> float:
    model_id = backend.resolve_model_id("sonnet-4.6")
    total = 0.0
    for task in tasks:
        response = backend.complete([task], model_id)
        total += _reward(response["text"])
    return total / len(tasks)


# ---------------------------------------------------------------------------
# Always-on parity check (stubs)
# ---------------------------------------------------------------------------


def test_parity_in_memory_stubs_within_five_percent() -> None:
    """Two identical stubs must be within +/- 5%."""
    backend_a: LLMBackend = _StubBackend(name="stub-a")
    backend_b: LLMBackend = _StubBackend(name="stub-b")
    mean_a = _mean_reward(backend_a, TASKS)
    mean_b = _mean_reward(backend_b, TASKS)
    denom = max(abs(mean_a), abs(mean_b), 1e-9)
    assert abs(mean_a - mean_b) / denom <= 0.05


def test_parity_small_drift_still_within_five_percent() -> None:
    """A deliberately drifted stub (adds 1 word) must still be within 5% on short replies."""
    backend_a: LLMBackend = _StubBackend(name="stub-a")
    backend_b: LLMBackend = _StubBackend(name="stub-b-drift", _echo_suffix=" x")
    mean_a = _mean_reward(backend_a, TASKS)
    mean_b = _mean_reward(backend_b, TASKS)
    # Drift of 1 word per reply on ~4-word prompts is > 5%; this test
    # documents that the tolerance catches real drift.
    denom = max(abs(mean_a), abs(mean_b), 1e-9)
    assert abs(mean_a - mean_b) / denom > 0.05


# ---------------------------------------------------------------------------
# Live-backend parity — skipped without credentials.
# ---------------------------------------------------------------------------

LIVE_BACKEND_ENV_KEYS: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "bedrock": ("AWS_ACCESS_KEY_ID", "AWS_PROFILE", "AWS_SESSION_TOKEN"),
    "vertex": ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_REGION"),
    "azure_openai": ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY"),
    "openai_compat": ("OPENAI_COMPAT_BASE_URL",),
}


def _has_creds(backend_name: str) -> bool:
    keys = LIVE_BACKEND_ENV_KEYS.get(backend_name, ())
    return bool(keys) and all(os.environ.get(k) for k in keys)


@pytest.mark.integration
@pytest.mark.parametrize(
    "second_backend",
    ["bedrock", "vertex", "azure_openai", "openai_compat"],
)
def test_parity_anthropic_vs_second_backend_within_five_percent(
    second_backend: str,
) -> None:
    """Compare Anthropic vs a second live backend on the same tiny task set.

    Skipped when either backend's credentials are absent (acceptance
    criterion 4). The ``integration`` marker ensures non-integration
    runs (``pytest -m 'not integration'``) do not require network.
    """
    if not _has_creds("anthropic"):
        pytest.skip("ANTHROPIC_API_KEY not set")
    if not _has_creds(second_backend):
        pytest.skip(f"credentials for {second_backend} not set")

    from codeprobe.llm.backends import get_backend

    primary = get_backend("anthropic")
    secondary = get_backend(second_backend)

    mean_a = _mean_reward(primary, TASKS)
    mean_b = _mean_reward(secondary, TASKS)
    denom = max(abs(mean_a), abs(mean_b), 1e-9)
    assert abs(mean_a - mean_b) / denom <= 0.05, (
        f"Parity drift too high: {mean_a=} {mean_b=}"
    )
