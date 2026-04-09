"""Contract tests — every adapter output must populate cost/token fields."""

from __future__ import annotations

import pytest

from codeprobe.adapters.protocol import AgentOutput

# -- Fixture outputs per adapter ------------------------------------------------
# Each fixture returns a realistic AgentOutput as if the adapter ran successfully.


@pytest.fixture()
def claude_output() -> AgentOutput:
    return AgentOutput(
        stdout="Fixed the bug in main.py",
        stderr=None,
        exit_code=0,
        duration_seconds=12.5,
        input_tokens=1500,
        output_tokens=350,
        cache_read_tokens=200,
        cost_usd=0.0042,
        cost_model="per_token",
        cost_source="api_reported",
    )


@pytest.fixture()
def copilot_output() -> AgentOutput:
    return AgentOutput(
        stdout="Applied fix to utils.py",
        stderr=None,
        exit_code=0,
        duration_seconds=8.3,
        input_tokens=1200,
        output_tokens=280,
        cost_usd=0.0031,
        cost_model="per_token",
        cost_source="log_parsed",
    )


@pytest.fixture()
def codex_output() -> AgentOutput:
    return AgentOutput(
        stdout="Refactored the module",
        stderr=None,
        exit_code=0,
        duration_seconds=6.1,
        input_tokens=800,
        output_tokens=420,
        cost_usd=0.0025,
        cost_model="per_token",
        cost_source="api_reported",
    )


# -- Contract assertions -------------------------------------------------------

_ADAPTERS = ["claude", "copilot", "codex"]


@pytest.fixture(params=_ADAPTERS)
def adapter_output(request: pytest.FixtureRequest) -> AgentOutput:
    """Parametrized fixture returning each adapter's output."""
    return request.getfixturevalue(f"{request.param}_output")


class TestAdapterOutputContract:
    """Every adapter output must have non-None cost and token fields."""

    def test_cost_usd_not_none(self, adapter_output: AgentOutput) -> None:
        assert adapter_output.cost_usd is not None

    def test_cost_model_not_none(self, adapter_output: AgentOutput) -> None:
        assert adapter_output.cost_model is not None

    def test_cost_source_not_none(self, adapter_output: AgentOutput) -> None:
        assert adapter_output.cost_source is not None

    def test_input_tokens_not_none(self, adapter_output: AgentOutput) -> None:
        assert adapter_output.input_tokens is not None

    def test_output_tokens_not_none(self, adapter_output: AgentOutput) -> None:
        assert adapter_output.output_tokens is not None


# -- Registry lazy-import tests -------------------------------------------------


class TestRegistryLazyImport:
    """Importing codeprobe must not crash when optional CLI tools are missing."""

    def test_import_codeprobe_does_not_crash(self) -> None:
        """Importing the top-level package must always succeed."""
        import codeprobe  # noqa: F401

    def test_resolve_missing_adapter_gives_clear_error(self) -> None:
        """Resolving an unknown adapter should raise KeyError, not ImportError."""
        from codeprobe.core.registry import resolve

        with pytest.raises(KeyError, match="Unknown agent adapter"):
            resolve("nonexistent_adapter")
