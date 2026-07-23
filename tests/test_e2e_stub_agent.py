"""Protocol-conformance test for tests/fixtures/e2e_stub_agent.

The package is test-only infrastructure for the self-serve E2E harness
(never installed in dev/CI's normal environment, never shipped in the
codeprobe wheel), so it isn't on this repo's normal import path. Loaded via
``importlib.util.spec_from_file_location`` rather than a ``sys.path``
insert or a real ``pip install -e`` — a sys.path insert would also expose
that directory to ``importlib.metadata.entry_points()`` scanning for the
rest of the process, which is exactly how a contributor's local ``pip
install -e tests/fixtures/e2e_stub_agent`` (the same command the harness
itself runs, just pointed at a dev checkout) leaves a stray
``*.egg-info/`` there that silently registers ``e2e-stub`` as a real agent
for every subsequent test in the session, breaking anything that
enumerates ``codeprobe.core.registry.available()``
(``tests/test_cli.py::test_run_help_agent_lists_codex_from_registry``,
caught the hard way authoring this bead).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_STUB_AGENT_INIT = (
    Path(__file__).resolve().parent / "fixtures" / "e2e_stub_agent" / "e2e_stub_agent" / "__init__.py"
)
_SPEC = importlib.util.spec_from_file_location("e2e_stub_agent", _STUB_AGENT_INIT)
assert _SPEC is not None and _SPEC.loader is not None
e2e_stub_agent = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = e2e_stub_agent
_SPEC.loader.exec_module(e2e_stub_agent)

StubAdapter = e2e_stub_agent.StubAdapter
_pick_edit_target = e2e_stub_agent._pick_edit_target

from codeprobe.adapters.protocol import (  # noqa: E402
    AgentAdapter,
    AgentConfig,
    AgentOutput,
)


def test_stub_adapter_is_agent_adapter() -> None:
    adapter = StubAdapter()
    assert isinstance(adapter, AgentAdapter)
    assert adapter.name == "e2e-stub"


def test_stub_adapter_no_arg_constructible() -> None:
    StubAdapter()  # must not raise


def test_stub_adapter_capabilities_declare_only_what_run_consumes() -> None:
    caps = StubAdapter().capabilities
    assert caps.mcp_config is True
    assert caps.workspace_cwd is True
    # Everything else stays fail-closed False — grep-verified against
    # run(): no allowed_tools/disallowed_tools/max_turns/permission_mode/
    # timeout knob is read anywhere in this adapter.
    assert caps.allowed_tools is False
    assert caps.disallowed_tools is False
    assert caps.max_turns is False
    assert caps.permission_mode is False
    assert caps.timeout is False


def test_stub_adapter_preflight_requires_cwd() -> None:
    adapter = StubAdapter()
    issues = adapter.preflight(AgentConfig())
    assert issues  # non-empty: no cwd set


def test_stub_adapter_preflight_passes_with_cwd(tmp_path) -> None:
    adapter = StubAdapter()
    assert adapter.preflight(AgentConfig(cwd=str(tmp_path))) == []


def test_stub_adapter_isolate_session_returns_empty_dict() -> None:
    assert StubAdapter().isolate_session(0) == {}


def test_stub_adapter_run_returns_honest_agent_output(tmp_path) -> None:
    (tmp_path / "feature_1.py").write_text("def feature_1():\n    pass\n")
    adapter = StubAdapter()

    output = adapter.run("do the task", AgentConfig(cwd=str(tmp_path)))

    assert isinstance(output, AgentOutput)
    assert output.exit_code == 0
    assert output.cost_source == "unavailable"
    assert output.cost_usd == 0.0
    assert output.cost_model == "unknown"


def test_stub_adapter_run_edits_the_target_file(tmp_path) -> None:
    target = tmp_path / "feature_1.py"
    target.write_text("def feature_1():\n    pass\n")
    adapter = StubAdapter()

    adapter.run("do the task", AgentConfig(cwd=str(tmp_path)))

    assert "codeprobe-e2e-stub" in target.read_text()


def test_stub_adapter_run_reports_failure_on_missing_workspace(tmp_path) -> None:
    adapter = StubAdapter()
    missing = tmp_path / "does-not-exist"

    output = adapter.run("do the task", AgentConfig(cwd=str(missing)))

    assert output.exit_code != 0
    assert output.error is not None
    assert output.error_terminal is True


def test_stub_adapter_run_reports_failure_on_unset_cwd() -> None:
    adapter = StubAdapter()
    output = adapter.run("do the task", AgentConfig(cwd=None))
    assert output.exit_code != 0
    assert output.error is not None


@pytest.mark.parametrize("mcp_config", [None, {"mcpServers": {}}])
def test_stub_adapter_run_reports_mcp_config_presence_in_stdout(tmp_path, mcp_config) -> None:
    (tmp_path / "feature_1.py").write_text("x = 1\n")
    adapter = StubAdapter()
    output = adapter.run(
        "do the task", AgentConfig(cwd=str(tmp_path), mcp_config=mcp_config)
    )
    expected = "mcp_config=set" if mcp_config else "mcp_config=unset"
    assert expected in output.stdout


def test_pick_edit_target_excludes_tests_dir(tmp_path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_feature_1.py").write_text("def test_x(): pass\n")
    (tmp_path / "feature_1.py").write_text("def feature_1(): pass\n")

    target = _pick_edit_target(tmp_path)

    assert target is not None
    assert target.name == "feature_1.py"


def test_pick_edit_target_excludes_test_prefixed_files_anywhere(tmp_path) -> None:
    (tmp_path / "test_helpers.py").write_text("def test_h(): pass\n")

    assert _pick_edit_target(tmp_path) is None


def test_pick_edit_target_returns_none_for_empty_workspace(tmp_path) -> None:
    assert _pick_edit_target(tmp_path) is None


def test_pick_edit_target_is_deterministic_across_calls(tmp_path) -> None:
    (tmp_path / "b_module.py").write_text("B = 1\n")
    (tmp_path / "a_module.py").write_text("A = 1\n")

    assert _pick_edit_target(tmp_path) == _pick_edit_target(tmp_path) == tmp_path / "a_module.py"
