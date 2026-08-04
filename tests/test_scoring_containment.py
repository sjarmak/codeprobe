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
from importlib.metadata import version as package_version
from pathlib import Path

import pytest

import codeprobe.core.scoring.sandbox as scoring_sandbox
from codeprobe.core import containment
from codeprobe.core.scoring import BinaryScorer, CheckpointScorer
from codeprobe.core.scoring.sandbox import _run_in_sandbox, scorer_env_override
from codeprobe.sandbox import runner as container_runner
from codeprobe.sandbox.runner import SandboxError, SandboxResult

LOCAL_SCORING_IMAGE = f"codeprobe-scoring:{package_version('codeprobe')}"
SCORING_IMAGE = (
    f"registry.example.test/platform/codeprobe/codeprobe-scoring:"
    f"{package_version('codeprobe')}"
)


@pytest.fixture(autouse=True)
def _reset_active_plan() -> None:
    """Isolate the context-local containment plan between tests."""
    containment.set_active_plan(None)


def _make_test_sh(task_dir: Path, body: str = "#!/bin/bash\nexit 0\n") -> Path:
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    script = tests_dir / "test.sh"
    script.write_text(body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _ok_result(stdout: str = "") -> SandboxResult:
    return SandboxResult(stdout=stdout, stderr="", exit_code=0, duration_ms=5)


def _configure_scoring_image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(container_runner, "load_prepared_images", lambda: None)
    monkeypatch.setenv("CODEPROBE_SCORING_IMAGE", SCORING_IMAGE)


def _capture_prepared_tempdir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    sandbox_dir = tmp_path / "prepared-sandbox"

    def fake_mkdtemp(*_args: object, **_kwargs: object) -> str:
        sandbox_dir.mkdir()
        return str(sandbox_dir)

    monkeypatch.setattr(scoring_sandbox.tempfile, "mkdtemp", fake_mkdtemp)
    return sandbox_dir


def _patch_host_subprocess_failure(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> object:
        raise exc

    monkeypatch.setattr(scoring_sandbox.subprocess, "run", fake_run)


def _patch_prepare_failure(monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    if phase == "copytree":
        monkeypatch.setattr(
            scoring_sandbox.shutil,
            "copytree",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("copy failed")),
        )
        return
    real_write_text = Path.write_text

    def fake_write_text(path: Path, text: str, *args: object, **kwargs: object) -> int:
        if path.name == "agent_output.txt":
            raise OSError("write failed")
        return real_write_text(path, text, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fake_write_text)


def _patch_container_mode(
    monkeypatch: pytest.MonkeyPatch, stdout: str = ""
) -> None:
    _configure_scoring_image(monkeypatch)
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


def _capture_container_mode(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
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
            cmd=cmd, mounts=mounts, allow_writes=allow_writes, image=image,
            timeout=timeout, workdir=workdir, env=env
        )
        if env is not None and "TASK_REPO_ROOT" in env:
            task_repo_root = Path(env["TASK_REPO_ROOT"])
            calls["task_repo_marker"] = (task_repo_root / "marker").read_text()
            calls["worktree_leak_is_symlink"] = (
                task_repo_root / "worktree-leak"
            ).is_symlink()
        if workdir is not None:
            calls["task_leak_is_symlink"] = (
                Path(workdir) / "task-leak"
            ).is_symlink()
        return _ok_result()

    _configure_scoring_image(monkeypatch)
    monkeypatch.setattr(container_runner, "detect_engine", lambda: "/usr/bin/docker")
    monkeypatch.setattr(container_runner, "image_available", lambda *_: True)
    monkeypatch.setattr(container_runner, "run_in_sandbox", fake_run_in_sandbox)
    return calls


def _assert_container_invocation(run: object, calls: dict[str, object]) -> None:
    assert getattr(run, "execution_mode") == "container"
    assert getattr(run, "returncode") == 0
    assert getattr(run, "error") is None
    mounts = calls["mounts"]
    assert isinstance(mounts, dict)
    ((host, cont),) = mounts.items()
    assert host == cont
    assert calls["allow_writes"] is True
    assert calls["workdir"] == str(Path(host) / "task")
    assert calls["image"] == SCORING_IMAGE
    assert calls["timeout"] == 123.0
    _assert_container_env_and_cmd(calls, host)


def _assert_container_env_and_cmd(calls: dict[str, object], host: str) -> None:
    env = calls["env"]
    assert isinstance(env, dict)
    assert set(env) == {"AGENT_OUTPUT"}
    assert env["AGENT_OUTPUT"].startswith(host)
    cmd = calls["cmd"]
    assert isinstance(cmd, list)
    assert cmd[:2] == ["bash", str(Path(host) / "task" / "tests" / "test.sh")]


class TestContainerPath:
    def test_container_invocation_contract(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task_dir = tmp_path / "task"
        script = _make_test_sh(task_dir)
        calls = _capture_container_mode(monkeypatch)

        run = _run_in_sandbox(script, "agent says", task_dir, timeout=123)

        _assert_container_invocation(run, calls)

    def test_container_forwards_scorer_environment_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task_dir = tmp_path / "task"
        script = _make_test_sh(task_dir)
        outside_secret = tmp_path / "outside-secret"
        outside_secret.write_text("must stay outside")
        (task_dir / "task-leak").symlink_to(outside_secret)
        worktree = tmp_path / "agent-worktree"
        worktree.mkdir()
        (worktree / "marker").write_text("agent edit")
        (worktree / "worktree-leak").symlink_to(outside_secret)
        calls = _capture_container_mode(monkeypatch)

        with scorer_env_override({"TASK_REPO_ROOT": str(worktree)}):
            run = _run_in_sandbox(script, "agent says", task_dir, timeout=123)

        assert run.execution_mode == "container"
        env = calls["env"]
        assert isinstance(env, dict)
        task_repo_root = Path(env["TASK_REPO_ROOT"])
        assert task_repo_root != worktree
        mounts = calls["mounts"]
        assert isinstance(mounts, dict)
        assert any(task_repo_root.is_relative_to(Path(root)) for root in mounts)
        assert calls["task_repo_marker"] == "agent edit"
        assert calls["task_leak_is_symlink"] is True
        assert calls["worktree_leak_is_symlink"] is True
        assert set(env) == {"AGENT_OUTPUT", "TASK_REPO_ROOT"}

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
        _configure_scoring_image(monkeypatch)

        run = _run_in_sandbox(script, "output", task_dir)

        assert run.returncode == -1
        assert run.error is not None
        assert "engine exploded" in run.error
        assert run.execution_mode == "container"
        assert run.stderr == ""


class TestHostFallback:
    def test_host_path_with_consent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task_dir = tmp_path / "task"
        script = _make_test_sh(task_dir, "#!/bin/bash\necho ok\nexit 0\n")
        worktree = tmp_path / "agent-worktree"
        worktree.mkdir()
        monkeypatch.setattr(container_runner, "detect_engine", lambda: None)
        containment.set_active_plan(
            containment.ContainmentPlan(mode="host-consented")
        )

        captured: dict[str, object] = {}
        real_run = subprocess.run

        def spy(argv: list[str], **kwargs: object) -> object:
            captured["argv"] = argv
            captured["env"] = kwargs.get("env")
            return real_run(argv, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(
            "codeprobe.core.scoring.sandbox.subprocess.run", spy
        )

        with scorer_env_override({"TASK_REPO_ROOT": str(worktree)}):
            run = _run_in_sandbox(script, "output", task_dir)

        assert run.execution_mode == "host"
        assert run.returncode == 0
        assert run.stdout.strip() == "ok"
        argv = captured["argv"]
        assert isinstance(argv, list)
        assert argv[0] == "bash"
        env = captured["env"]
        assert isinstance(env, dict)
        assert env["TASK_REPO_ROOT"] == str(worktree)

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
    def test_container_plan_refuses_when_engine_disappears(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task_dir = tmp_path / "task"
        marker = tmp_path / "host-executed"
        script = _make_test_sh(task_dir, f"#!/bin/bash\ntouch '{marker}'\n")
        containment.set_active_plan(
            containment.ContainmentPlan(mode="container", engine="/usr/bin/docker")
        )
        monkeypatch.setattr(container_runner, "detect_engine", lambda: None)

        run = _run_in_sandbox(script, "output", task_dir)

        assert run.returncode == -1
        assert run.verifier_error is True
        assert run.execution_mode == "none"
        assert run.error is not None
        assert "container engine is no longer available" in run.error
        assert not marker.exists()

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
        monkeypatch.setattr(container_runner, "load_prepared_images", lambda: None)
        containment.set_active_plan(
            containment.ContainmentPlan(mode="sandboxed")
        )

        run = _run_in_sandbox(script, "output", task_dir)

        assert run.verifier_error is True
        assert run.returncode == -1
        assert run.error is not None
        assert "scoring image is not configured" in run.error
        assert "codeprobe bootstrap" in run.error
        assert "Dockerfile" not in run.error
        assert "CODEPROBE_SCORING_IMAGE" in run.error
        # Nothing executed — the disclosure must not claim host execution.
        assert run.execution_mode == "none"
        assert run.stderr == run.error

    def test_invalid_image_config_fails_closed_under_sandboxed_plan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task_dir = tmp_path / "task"
        script = _make_test_sh(task_dir)
        monkeypatch.setattr(
            container_runner, "detect_engine", lambda: "/usr/bin/docker"
        )
        monkeypatch.setenv("CODEPROBE_IMAGE_VERSION", "invalid tag")
        containment.set_active_plan(
            containment.ContainmentPlan(mode="sandboxed")
        )

        run = _run_in_sandbox(script, "output", task_dir)

        assert run.returncode == -1
        assert run.verifier_error is True
        assert run.execution_mode == "none"
        assert run.error is not None
        assert "CODEPROBE_IMAGE_VERSION" in run.error
        assert "CODEPROBE_SCORING_IMAGE" in run.error
        assert "codeprobe bootstrap" in run.error
        assert "Dockerfile" not in run.error


class TestNothingExecutedDisclosure:
    """Paths where no script process ran must disclose ``"none"``."""

    def test_materialization_failure_reports_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from codeprobe.core import scoring as scoring_pkg
        from codeprobe.core.scoring.materialize import AgentState

        task_dir = tmp_path / "task"
        script = _make_test_sh(task_dir)
        monkeypatch.setattr(scoring_pkg, "_is_git_repo", lambda p: True)
        monkeypatch.setattr(
            scoring_pkg,
            "_materialize_workspace",
            lambda ws, sd: (None, "git apply failed"),
        )

        run = _run_in_sandbox(
            script,
            "output",
            task_dir,
            agent_state=AgentState(base_commit="a" * 40, workspace=tmp_path),
        )

        assert run.verifier_error is True
        assert run.execution_mode == "none"
        assert run.stderr == run.error

    def test_binary_scorer_reports_none_on_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal trial never ran anything; details must say so."""
        task_dir = tmp_path / "task"
        _make_test_sh(task_dir)
        monkeypatch.setattr(
            container_runner, "detect_engine", lambda: "/usr/bin/docker"
        )
        monkeypatch.setattr(
            container_runner, "image_available", lambda engine, image: False
        )
        containment.set_active_plan(
            containment.ContainmentPlan(mode="sandboxed")
        )

        result = BinaryScorer().score("output", task_dir)

        assert result.passed is False
        assert result.verdict == "verifier_error"
        assert result.details["sandbox_execution"] == "none"


class TestSandboxFailureCleanup:
    @pytest.mark.parametrize(
        ("exc", "expected_error", "expected_mode"),
        [
            (subprocess.TimeoutExpired(cmd=["bash"], timeout=1), "Scoring timed out", "host"),
            (PermissionError("secret path /tmp/leaky"), "Sandbox setup failed", "none"),
        ],
    )
    def test_host_failure_cleans_prepared_sandbox(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        exc: BaseException,
        expected_error: str,
        expected_mode: str,
    ) -> None:
        task_dir = tmp_path / "task"
        script = _make_test_sh(task_dir)
        sandbox_dir = _capture_prepared_tempdir(monkeypatch, tmp_path)
        monkeypatch.setattr(container_runner, "detect_engine", lambda: None)
        _patch_host_subprocess_failure(monkeypatch, exc)

        run = _run_in_sandbox(script, "output", task_dir)

        assert run.error == expected_error
        assert run.stderr == ""
        assert run.execution_mode == expected_mode
        assert not sandbox_dir.exists()

    @pytest.mark.parametrize("phase", ["copytree", "write-output"])
    def test_prepare_failure_cleans_created_tempdir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
    ) -> None:
        task_dir = tmp_path / "task"
        script = _make_test_sh(task_dir)
        sandbox_dir = _capture_prepared_tempdir(monkeypatch, tmp_path)
        _patch_prepare_failure(monkeypatch, phase)

        run = _run_in_sandbox(script, "output", task_dir)

        assert run.error == "Sandbox setup failed"
        assert run.stderr == ""
        assert not sandbox_dir.exists()


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
