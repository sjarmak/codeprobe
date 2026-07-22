"""Tests for containerized scoring execution (codeprobe-f7rl.4).

Mined test/verifier scripts are third-party code. ``_run_in_sandbox`` must
execute them inside the ``--network=none`` scoring container whenever an
engine and the scoring image are available, fall back to host bash only
with consent (``host-consented`` plan or plan-less library use), and
disclose the execution mode on every scored trial.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from codeprobe.core import containment
from codeprobe.core.scoring import BinaryScorer, CheckpointScorer
from codeprobe.core.scoring.sandbox import _run_in_sandbox
from codeprobe.sandbox import runner as container_runner
from codeprobe.sandbox.runner import SandboxError, SandboxResult


@pytest.fixture(autouse=True)
def _reset_active_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the module-global containment plan between tests."""
    monkeypatch.setattr(containment, "_active_plan", None)


def _make_test_sh(task_dir: Path, body: str = "#!/bin/bash\nexit 0\n") -> Path:
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    script = tests_dir / "test.sh"
    script.write_text(body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _ok_result(stdout: str = "") -> SandboxResult:
    return SandboxResult(stdout=stdout, stderr="", exit_code=0, duration_ms=5)


def _patch_container_mode(
    monkeypatch: pytest.MonkeyPatch, stdout: str = ""
) -> None:
    monkeypatch.setattr(
        container_runner, "detect_engine", lambda: "/usr/bin/docker"
    )
    monkeypatch.setattr(
        container_runner, "image_available", lambda engine, image: True
    )
    monkeypatch.setattr(
        container_runner,
        "run_in_sandbox",
        lambda *args, **kwargs: _ok_result(stdout),
    )


class TestContainerPath:
    def test_container_invocation_contract(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task_dir = tmp_path / "task"
        script = _make_test_sh(task_dir)
        calls: dict[str, object] = {}

        def fake_run_in_sandbox(
            cmd: list[str] | str,
            mounts: dict[str, str],
            *,
            allow_writes: bool = False,
            image: str = "",
            timeout: float = 0.0,
            workdir: str | None = None,
            env: dict[str, str] | None = None,
        ) -> SandboxResult:
            calls.update(
                cmd=cmd,
                mounts=mounts,
                allow_writes=allow_writes,
                image=image,
                timeout=timeout,
                workdir=workdir,
                env=env,
            )
            return _ok_result()

        monkeypatch.setattr(
            container_runner, "detect_engine", lambda: "/usr/bin/docker"
        )
        monkeypatch.setattr(
            container_runner, "image_available", lambda engine, image: True
        )
        monkeypatch.setattr(
            container_runner, "run_in_sandbox", fake_run_in_sandbox
        )

        run = _run_in_sandbox(script, "agent says", task_dir, timeout=123)

        assert run.execution_mode == "container"
        assert run.returncode == 0
        assert run.error is None

        # Identity mount: host path == container path, single mount over
        # the sandbox dir, so env paths stay valid without translation.
        mounts = calls["mounts"]
        assert isinstance(mounts, dict)
        ((host, cont),) = mounts.items()
        assert host == cont

        assert calls["allow_writes"] is True
        assert calls["workdir"] == str(Path(host) / "task")
        assert calls["image"] == container_runner.DEFAULT_SCORING_IMAGE
        assert calls["timeout"] == 123.0

        # env is limited to env_extra — the image supplies its own PATH.
        env = calls["env"]
        assert isinstance(env, dict)
        assert set(env) == {"AGENT_OUTPUT"}
        assert env["AGENT_OUTPUT"].startswith(host)

        cmd = calls["cmd"]
        assert isinstance(cmd, list)
        assert cmd[0] == "bash"
        assert cmd[1] == str(Path(host) / "task" / "tests" / "test.sh")

    def test_sandbox_error_maps_to_error_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task_dir = tmp_path / "task"
        script = _make_test_sh(task_dir)

        def boom(*args: object, **kwargs: object) -> SandboxResult:
            raise SandboxError("engine exploded")

        monkeypatch.setattr(
            container_runner, "detect_engine", lambda: "/usr/bin/docker"
        )
        monkeypatch.setattr(
            container_runner, "image_available", lambda engine, image: True
        )
        monkeypatch.setattr(container_runner, "run_in_sandbox", boom)

        run = _run_in_sandbox(script, "output", task_dir)

        assert run.returncode == -1
        assert run.error is not None
        assert "engine exploded" in run.error
        assert run.execution_mode == "container"


class TestHostFallback:
    def test_host_path_with_consent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task_dir = tmp_path / "task"
        script = _make_test_sh(task_dir, "#!/bin/bash\necho ok\nexit 0\n")
        monkeypatch.setattr(container_runner, "detect_engine", lambda: None)
        containment.set_active_plan(
            containment.ContainmentPlan(mode="host-consented")
        )

        captured: dict[str, object] = {}
        real_run = subprocess.run

        def spy(argv: list[str], **kwargs: object) -> object:
            captured["argv"] = argv
            return real_run(argv, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(
            "codeprobe.core.scoring.sandbox.subprocess.run", spy
        )

        run = _run_in_sandbox(script, "output", task_dir)

        assert run.execution_mode == "host"
        assert run.returncode == 0
        assert run.stdout.strip() == "ok"
        argv = captured["argv"]
        assert isinstance(argv, list)
        assert argv[0] == "bash"

    def test_host_path_planless_library_use(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task_dir = tmp_path / "task"
        script = _make_test_sh(task_dir)
        monkeypatch.setattr(container_runner, "detect_engine", lambda: None)

        run = _run_in_sandbox(script, "output", task_dir)

        assert run.execution_mode == "host"
        assert run.returncode == 0
        assert run.verifier_error is False

    def test_planless_with_engine_but_no_image_stays_host(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Library/test callers on a docker machine keep current behavior."""
        task_dir = tmp_path / "task"
        script = _make_test_sh(task_dir)
        monkeypatch.setattr(
            container_runner, "detect_engine", lambda: "/usr/bin/docker"
        )
        monkeypatch.setattr(
            container_runner, "image_available", lambda engine, image: False
        )

        run = _run_in_sandbox(script, "output", task_dir)

        assert run.execution_mode == "host"
        assert run.returncode == 0
        assert run.verifier_error is False


class TestMissingImageRefusal:
    def test_engine_without_image_refused_under_sandboxed_plan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task_dir = tmp_path / "task"
        script = _make_test_sh(task_dir)
        monkeypatch.setattr(
            container_runner, "detect_engine", lambda: "/usr/bin/docker"
        )
        monkeypatch.setattr(
            container_runner, "image_available", lambda engine, image: False
        )
        containment.set_active_plan(
            containment.ContainmentPlan(mode="sandboxed")
        )

        run = _run_in_sandbox(script, "output", task_dir)

        assert run.verifier_error is True
        assert run.returncode == -1
        assert run.error is not None
        assert (
            "docker build -f src/codeprobe/sandbox/Dockerfile.scoring "
            "-t codeprobe-scoring:0.12 ." in run.error
        )


class TestScorerDisclosure:
    def test_binary_scorer_reports_container(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task_dir = tmp_path / "task"
        _make_test_sh(task_dir)
        _patch_container_mode(monkeypatch)

        result = BinaryScorer().score("output", task_dir)

        assert result.score == 1.0
        assert result.details["sandbox_execution"] == "container"

    def test_binary_scorer_reports_host(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task_dir = tmp_path / "task"
        _make_test_sh(task_dir)
        monkeypatch.setattr(container_runner, "detect_engine", lambda: None)

        result = BinaryScorer().score("output", task_dir)

        assert result.score == 1.0
        assert result.details["sandbox_execution"] == "host"

    def test_checkpoint_scorer_reports_container(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task_dir = tmp_path / "task"
        verifiers_dir = task_dir / "tests" / "verifiers"
        verifiers_dir.mkdir(parents=True)
        verifier = verifiers_dir / "check.sh"
        verifier.write_text("#!/bin/bash\nexit 0\n")
        verifier.chmod(verifier.stat().st_mode | stat.S_IEXEC)
        _patch_container_mode(monkeypatch, stdout='{"score": 1.0}')

        scorer = CheckpointScorer(
            metadata_checkpoints=[
                {"name": "check", "weight": 1.0, "verifier": "check.sh"}
            ]
        )
        result = scorer.score("output", task_dir)

        assert result.score == 1.0
        assert result.details["sandbox_execution"] == "container"
