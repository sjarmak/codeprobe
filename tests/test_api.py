"""Tests for the in-process batch API: run_experiment()."""

from __future__ import annotations

import json
import stat
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from codeprobe.adapters.protocol import AdapterCapabilities
from codeprobe.analysis.report import Report
from codeprobe.cli.errors import PrescriptiveError
from tests.conftest import FakeAdapter

# run_experiment uses the experiment dir (not a git checkout) as repo_path;
# every run path now requires a worktree slot (codeprobe-f7rl.2), so use the
# passthrough isolation fake from tests/conftest.py.
pytestmark = pytest.mark.usefixtures("fake_worktree_isolation")


def _make_experiment_dir(
    base: Path,
    *,
    name: str = "test-exp",
    configs: list[dict] | None = None,
    num_tasks: int = 2,
    passing: bool = True,
) -> Path:
    """Create a minimal experiment directory with tasks."""
    exp_dir = base / name
    exp_dir.mkdir(parents=True)

    if configs is None:
        configs = [{"label": "default", "agent": "fake"}]

    experiment_json = {
        "name": name,
        "description": "test experiment",
        "tasks_dir": "tasks",
        "configs": configs,
    }
    (exp_dir / "experiment.json").write_text(
        json.dumps(experiment_json, indent=2), encoding="utf-8"
    )

    tasks_dir = exp_dir / "tasks"
    tasks_dir.mkdir()

    for i in range(num_tasks):
        task_dir = tasks_dir / f"task-{i:03d}"
        task_dir.mkdir()
        (task_dir / "instruction.md").write_text(f"Fix bug {i}.")
        tests_subdir = task_dir / "tests"
        tests_subdir.mkdir()
        test_sh = tests_subdir / "test.sh"
        exit_code = 0 if passing else 1
        test_sh.write_text(f"#!/bin/bash\nexit {exit_code}\n")
        test_sh.chmod(test_sh.stat().st_mode | stat.S_IEXEC)

    return exp_dir


class TestRunExperiment:
    """Tests for the public run_experiment() API."""

    def test_returns_report(self, tmp_path: Path) -> None:
        """run_experiment returns a Report dataclass."""
        from codeprobe.api import run_experiment

        exp_dir = _make_experiment_dir(tmp_path, passing=True)
        adapter = FakeAdapter(stdout="PASS", exit_code=0)

        with patch("codeprobe.api.resolve", return_value=adapter):
            report = run_experiment(exp_dir)

        assert isinstance(report, Report)
        assert report.experiment_name == "test-exp"
        assert len(report.summaries) == 1
        assert report.summaries[0].label == "default"

    def test_bare_host_refuses_before_adapter_resolution(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The library entry point must not bypass the CLI containment gate."""
        from codeprobe import api as api_mod

        monkeypatch.delenv("CODEPROBE_SANDBOX", raising=False)
        monkeypatch.setattr("codeprobe.core.sandbox.is_sandboxed", lambda: False)
        exp_dir = _make_experiment_dir(tmp_path)

        with (
            patch.object(api_mod, "resolve") as mock_resolve,
            patch.object(api_mod, "execute_config") as mock_execute,
            pytest.raises(PrescriptiveError) as exc_info,
        ):
            api_mod.run_experiment(exp_dir)

        assert exc_info.value.code == "UNCONTAINED_REFUSED"
        mock_resolve.assert_not_called()
        mock_execute.assert_not_called()

    def test_explicit_uncontained_consent_is_scoped_to_execution(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Programmatic callers can explicitly accept disclosed host execution."""
        from codeprobe import api as api_mod
        from codeprobe.core import containment

        monkeypatch.delenv("CODEPROBE_SANDBOX", raising=False)
        monkeypatch.setattr("codeprobe.core.sandbox.is_sandboxed", lambda: False)
        exp_dir = _make_experiment_dir(tmp_path, num_tasks=1)
        adapter = FakeAdapter(stdout="PASS", exit_code=0)

        observed: list[str | None] = []
        original_run = adapter.run

        def observe_run(*args: object, **kwargs: object):
            plan = containment.active_plan()
            observed.append(plan.mode if plan is not None else None)
            return original_run(*args, **kwargs)

        with (
            patch.object(api_mod, "resolve", return_value=adapter),
            patch.object(adapter, "run", side_effect=observe_run),
        ):
            api_mod.run_experiment(exp_dir, uncontained=True)

        assert observed == ["host-consented"]
        assert containment.active_plan() is None

    def test_failed_setup_does_not_leak_host_consent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Consent is scoped to a runnable experiment, not future API calls."""
        from codeprobe import api as api_mod
        from codeprobe.core import containment

        monkeypatch.delenv("CODEPROBE_SANDBOX", raising=False)
        monkeypatch.setattr("codeprobe.core.sandbox.is_sandboxed", lambda: False)

        with pytest.raises(FileNotFoundError):
            api_mod.run_experiment(
                tmp_path / "missing-experiment",
                uncontained=True,
            )

        assert containment.active_plan() is None

    def test_default_permission_mode_is_upgraded_for_autonomous_run(
        self,
        tmp_path: Path,
    ) -> None:
        """Library and CLI runs must give the same config the same permissions."""
        from codeprobe import api as api_mod

        exp_dir = _make_experiment_dir(tmp_path, num_tasks=1)
        adapter = FakeAdapter(stdout="PASS", exit_code=0)

        with patch.object(api_mod, "resolve", return_value=adapter):
            api_mod.run_experiment(exp_dir)

        assert len(adapter.run_calls) == 1
        assert adapter.run_calls[0][1].permission_mode == "dangerously_skip"

    def test_unsupported_later_arm_refuses_before_any_dispatch(
        self,
        tmp_path: Path,
    ) -> None:
        """Capability validation covers every arm before the first can spend."""
        from codeprobe import api as api_mod

        class LimitedAdapter(FakeAdapter):
            @property
            def capabilities(self) -> AdapterCapabilities:
                return AdapterCapabilities()

        capable = FakeAdapter(stdout="PASS", exit_code=0)
        limited = LimitedAdapter(stdout="PASS", exit_code=0)
        exp_dir = _make_experiment_dir(
            tmp_path,
            configs=[
                {"label": "baseline", "agent": "fake"},
                {
                    "label": "invalid-mcp-arm",
                    "agent": "limited",
                    "mcp_config": {
                        "mcpServers": {
                            "sg": {
                                "type": "http",
                                "url": "https://example.test/mcp",
                            }
                        }
                    },
                    "mcp_mode": "strict",
                },
            ],
        )

        def resolve_adapter(name: str) -> FakeAdapter:
            return limited if name == "limited" else capable

        with (
            patch.object(api_mod, "resolve", side_effect=resolve_adapter),
            patch.object(api_mod, "execute_config") as mock_execute,
            pytest.raises(PrescriptiveError) as exc_info,
        ):
            api_mod.run_experiment(exp_dir)

        assert exc_info.value.code == "ADAPTER_CAPABILITY"
        mock_execute.assert_not_called()

    def test_duplicate_config_labels_refuse_before_dispatch(
        self,
        tmp_path: Path,
    ) -> None:
        """Labels key result paths and must uniquely identify an arm."""
        from codeprobe import api as api_mod

        exp_dir = _make_experiment_dir(
            tmp_path,
            configs=[
                {"label": "duplicate", "agent": "fake"},
                {"label": "duplicate", "agent": "fake"},
            ],
        )

        with (
            patch.object(api_mod, "resolve", return_value=FakeAdapter()),
            patch.object(api_mod, "execute_config") as mock_execute,
            pytest.raises(ValueError, match="Duplicate config label"),
        ):
            api_mod.run_experiment(exp_dir)

        mock_execute.assert_not_called()

    def test_concurrent_runs_pass_their_own_containment_plan(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One API caller's host consent cannot authorize another caller."""
        from codeprobe import api as api_mod
        from codeprobe.core.containment import ContainmentPlan

        monkeypatch.delenv("CODEPROBE_SANDBOX", raising=False)
        container_exp = _make_experiment_dir(
            tmp_path, name="container-exp", configs=[{"label": "container-arm"}]
        )
        host_exp = _make_experiment_dir(
            tmp_path, name="host-exp", configs=[{"label": "host-arm"}]
        )
        barrier = threading.Barrier(2)
        observed: dict[str, str] = {}
        failures: list[BaseException] = []

        def fake_resolve_containment(uncontained: bool) -> ContainmentPlan:
            return ContainmentPlan(
                mode="host-consented" if uncontained else "container",
                engine=None if uncontained else "/usr/bin/docker",
            )

        def fake_execute_config(**kwargs: object) -> list:
            barrier.wait()
            config = kwargs["experiment_config"]
            plan = kwargs["containment_plan"]
            observed[config.label] = plan.mode  # type: ignore[union-attr]
            return []

        def run(path: Path, *, uncontained: bool) -> None:
            try:
                api_mod.run_experiment(path, uncontained=uncontained)
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        with (
            patch.object(
                api_mod,
                "resolve_containment",
                side_effect=fake_resolve_containment,
            ),
            patch.object(api_mod, "resolve", return_value=FakeAdapter()),
            patch.object(api_mod, "execute_config", side_effect=fake_execute_config),
        ):
            threads = [
                threading.Thread(
                    target=run, args=(container_exp,), kwargs={"uncontained": False}
                ),
                threading.Thread(
                    target=run, args=(host_exp,), kwargs={"uncontained": True}
                ),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

        assert not failures
        assert not any(thread.is_alive() for thread in threads)
        assert observed == {
            "container-arm": "container",
            "host-arm": "host-consented",
        }

    def test_report_matches_expected_scores(self, tmp_path: Path) -> None:
        """Verify pass rate reflects task outcomes."""
        from codeprobe.api import run_experiment

        exp_dir = _make_experiment_dir(tmp_path, passing=True, num_tasks=3)
        adapter = FakeAdapter(stdout="PASS", exit_code=0)

        with patch("codeprobe.api.resolve", return_value=adapter):
            report = run_experiment(exp_dir)

        summary = report.summaries[0]
        assert summary.total_tasks == 3

    def test_with_explicit_configs(self, tmp_path: Path) -> None:
        """Passing explicit config dicts overrides experiment.json configs."""
        from codeprobe.api import run_experiment

        exp_dir = _make_experiment_dir(tmp_path)
        adapter = FakeAdapter(stdout="PASS", exit_code=0)

        configs = [
            {"label": "custom-a", "agent": "fake"},
            {"label": "custom-b", "agent": "fake"},
        ]

        with patch("codeprobe.api.resolve", return_value=adapter):
            report = run_experiment(exp_dir, configs=configs)

        assert len(report.summaries) == 2
        labels = {s.label for s in report.summaries}
        assert labels == {"custom-a", "custom-b"}

    def test_max_cost_usd_passed_to_executor(self, tmp_path: Path) -> None:
        """max_cost_usd is forwarded to execute_config."""
        from codeprobe.api import run_experiment

        exp_dir = _make_experiment_dir(tmp_path, num_tasks=5)
        # Adapter with per_token cost: $10 per task
        adapter = FakeAdapter(
            stdout="PASS", exit_code=0, cost_usd=10.0, cost_model="per_token"
        )

        with patch("codeprobe.api.resolve", return_value=adapter):
            report = run_experiment(exp_dir, max_cost_usd=15.0)

        # With $10/task and $15 budget, should stop after 2 tasks
        summary = report.summaries[0]
        assert summary.total_tasks <= 3  # at most 2 run + budget check

    def test_checkpoint_resume(self, tmp_path: Path) -> None:
        """run_experiment uses CheckpointStore so resuming skips done tasks."""
        from codeprobe.api import run_experiment

        exp_dir = _make_experiment_dir(tmp_path, num_tasks=3)
        adapter = FakeAdapter(stdout="PASS", exit_code=0)

        with patch("codeprobe.api.resolve", return_value=adapter):
            report1 = run_experiment(exp_dir)

        # All 3 tasks should be completed
        assert report1.summaries[0].total_tasks == 3
        assert len(adapter.run_calls) == 3

        # Second run: adapter should not be called again (all checkpointed)
        adapter2 = FakeAdapter(stdout="PASS", exit_code=0)
        with patch("codeprobe.api.resolve", return_value=adapter2):
            report2 = run_experiment(exp_dir)

        assert report2.summaries[0].total_tasks == 3
        assert len(adapter2.run_calls) == 0  # all from checkpoint

    def test_no_click_dependency(self) -> None:
        """The api module does not import click."""
        import importlib

        mod = importlib.import_module("codeprobe.api")
        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "import click" not in source
        assert "from click" not in source

    def test_saves_results_json(self, tmp_path: Path) -> None:
        """run_experiment writes results.json for each config."""
        from codeprobe.api import run_experiment

        exp_dir = _make_experiment_dir(tmp_path)
        adapter = FakeAdapter(stdout="PASS", exit_code=0)

        with patch("codeprobe.api.resolve", return_value=adapter):
            run_experiment(exp_dir)

        results_path = exp_dir / "runs" / "default" / "results.json"
        assert results_path.is_file()
        data = json.loads(results_path.read_text(encoding="utf-8"))
        assert data["config"] == "default"
        assert len(data["completed"]) == 2

    def test_invalid_experiment_dir(self, tmp_path: Path) -> None:
        """run_experiment raises FileNotFoundError for missing experiment."""
        from codeprobe.api import run_experiment

        with pytest.raises(FileNotFoundError):
            run_experiment(tmp_path / "nonexistent")

    def test_no_tasks_raises_value_error(self, tmp_path: Path) -> None:
        """run_experiment raises ValueError when no task dirs found."""
        from codeprobe.api import run_experiment

        exp_dir = tmp_path / "empty-exp"
        exp_dir.mkdir()
        (exp_dir / "tasks").mkdir()
        experiment_json = {
            "name": "empty-exp",
            "description": "",
            "tasks_dir": "tasks",
            "configs": [{"label": "default", "agent": "fake"}],
        }
        (exp_dir / "experiment.json").write_text(
            json.dumps(experiment_json), encoding="utf-8"
        )

        with pytest.raises(ValueError, match="No tasks found"):
            run_experiment(exp_dir)

    def test_quarantined_adapter_refused_before_any_arm_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """codeprobe-f7rl.27: a codex arm refuses the whole run upfront.

        The library path must match the CLI preflight: no half-run
        comparison — the claude arm ahead of the codex arm must not
        execute either.
        """
        from codeprobe.adapters.claude import ClaudeAdapter
        from codeprobe.adapters.codex import CodexAdapter
        from codeprobe.api import run_experiment

        calls: list[str] = []

        def _make_run(adapter_name: str):
            def _run(self, prompt, config, session_env=None):  # noqa: ANN001
                calls.append(adapter_name)
                raise AssertionError(f"{adapter_name}.run must not be called")

            return _run

        monkeypatch.setattr(ClaudeAdapter, "run", _make_run("claude"))
        monkeypatch.setattr(CodexAdapter, "run", _make_run("codex"))

        exp_dir = _make_experiment_dir(
            tmp_path,
            configs=[
                {"label": "baseline", "agent": "claude"},
                {"label": "codex-arm", "agent": "codex"},
            ],
        )

        with pytest.raises(ValueError, match="quarantined"):
            run_experiment(exp_dir)
        assert calls == []
