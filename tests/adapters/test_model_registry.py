"""Tests for the known-model registry and ``--model`` validation
(codeprobe-8yjf / codeprobe-fvfo Gap 2)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from codeprobe.adapters.models import (
    known_agents,
    model_set,
    validate_model,
)
from codeprobe.cli.errors import PrescriptiveError


class TestResolve:
    def test_alias_resolves_to_canonical(self) -> None:
        ms = model_set("claude")
        assert ms is not None
        assert ms.resolve("opus") == "claude-opus-4-7"
        assert ms.resolve("sonnet") == "claude-sonnet-4-6"

    def test_full_id_known(self) -> None:
        ms = model_set("claude")
        assert ms is not None
        assert ms.resolve("claude-sonnet-4-6") == "claude-sonnet-4-6"

    def test_date_suffixed_id_tolerated(self) -> None:
        ms = model_set("claude")
        assert ms is not None
        assert ms.resolve("claude-haiku-4-5-20251001") == "claude-haiku-4-5"

    def test_structurally_valid_newer_id_accepted(self) -> None:
        # A real but not-yet-enumerated dated release must not be falsely
        # rejected — the id-shape fallback accepts it.
        ms = model_set("claude")
        assert ms is not None
        assert ms.resolve("claude-opus-4-9") == "claude-opus-4-9"

    def test_garbage_token_unknown(self) -> None:
        ms = model_set("claude")
        assert ms is not None
        # The exact tokens the cold-start audit hit.
        assert ms.resolve("opus-4") is None
        assert ms.resolve("sonnet-4") is None
        assert ms.resolve("gpt-4") is None
        assert ms.resolve("") is None


class TestValidateModel:
    def test_valid_token_passes(self) -> None:
        validate_model("claude", "claude-sonnet-4-6")
        validate_model("claude", "opus")  # alias

    def test_empty_token_is_noop(self) -> None:
        # Model is optional; the agent uses its own default.
        validate_model("claude", None)
        validate_model("claude", "")

    def test_unknown_token_raises_prescriptive(self) -> None:
        with pytest.raises(PrescriptiveError) as exc:
            validate_model("claude", "opus-4")
        err = exc.value
        assert err.code == "UNKNOWN_MODEL"
        assert err.next_try_flag == "--model"
        assert err.next_try_value == "claude-sonnet-4-6"
        # The message must enumerate valid tokens and point at models list.
        assert "opus" in err.message
        assert "codeprobe models list" in err.message
        assert err.detail["given"] == "opus-4"

    def test_advisory_agent_never_rejects(self) -> None:
        # codex/copilot model sets are advisory — a fluid vendor model must
        # not be falsely blocked.
        validate_model("codex", "gpt-6-some-future-id")
        validate_model("copilot", "anything")

    def test_unregistered_agent_is_noop(self) -> None:
        validate_model("aider", "whatever")


class TestRegistryConsistency:
    def test_all_agents_have_a_default_or_empty(self) -> None:
        for name in known_agents():
            ms = model_set(name)
            assert ms is not None
            assert isinstance(ms.default, str)

    def test_readme_model_examples_are_known(self) -> None:
        """Every `--model <token>` example in README.md must be a known
        claude token — prevents shipping another invalid example."""
        readme = Path(__file__).resolve().parents[2] / "README.md"
        text = readme.read_text(encoding="utf-8")
        tokens = re.findall(r"--model\s+([A-Za-z0-9._-]+)", text)
        assert tokens, "expected at least one --model example in README"
        ms = model_set("claude")
        assert ms is not None
        unknown = [t for t in tokens if not ms.is_known(t)]
        assert not unknown, f"README has unknown --model tokens: {unknown}"
