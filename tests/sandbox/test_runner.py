"""Tests for codeprobe.sandbox.runner and codeprobe.cli._sandbox.

Unit tests (always run) exercise the argv construction and error
translation via mocked subprocess. Docker-gated integration tests are
marked with :func:`pytest.mark.skipif` so the suite passes cleanly on
machines without docker installed.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from codeprobe.cli._sandbox import sandbox_options
from codeprobe.sandbox.runner import (
    SandboxError,
    SandboxResult,
    SandboxWriteDeniedError,
    _build_run_command,
    image_available,
    run_in_sandbox,
)

HAS_DOCKER = shutil.which("docker") is not None
DOCKERFILE = (
    Path(__file__).resolve().parent.parent.parent
    / "src"
    / "codeprobe"
    / "sandbox"
    / "Dockerfile.sg_only"
)
TEST_IMAGE_TAG = "docker.io/library/codeprobe-sandbox:sg-only-test"
UNIT_IMAGE = "registry.example.test/codeprobe-sandbox:sg-only"

# ---------------------------------------------------------------------------
# Argv construction (pure, no subprocess)
# ---------------------------------------------------------------------------


def test_build_run_command_uses_ro_mode_by_default() -> None:
    argv = _build_run_command(
        "docker",
        ["echo", "hi"],
        {"/host/src": "/workspace"},
        allow_writes=False,
        image=UNIT_IMAGE,
        workdir=None,
        env=None,
    )
    assert argv[0] == "docker"
    assert "run" in argv
    assert "--rm" in argv
    assert "--network=none" in argv
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges" in argv
    assert "--read-only" in argv
    assert "--pull=never" in argv
    assert "--cpus=2" in argv
    assert "--memory=4g" in argv
    assert "--memory-swap=4g" in argv
    assert ["-e", "HOME=/tmp"] == argv[argv.index("-e") : argv.index("-e") + 2]
    assert "/host/src:/workspace:ro" in argv
    assert "/host/src:/workspace:rw" not in argv
    assert argv[-2:] == ["echo", "hi"]


def test_build_run_command_uses_rw_mode_when_allowed() -> None:
    argv = _build_run_command(
        "docker",
        ["echo", "hi"],
        {"/host/src": "/workspace"},
        allow_writes=True,
        image=UNIT_IMAGE,
        workdir=None,
        env=None,
    )
    assert "/host/src:/workspace:rw" in argv
    assert "/host/src:/workspace:ro" not in argv


def test_build_run_command_network_parameter() -> None:
    """codeprobe-f7rl.5: ``network`` overrides the default ``--network=none``."""
    argv = _build_run_command(
        "docker",
        ["echo", "hi"],
        {},
        allow_writes=False,
        image=UNIT_IMAGE,
        workdir=None,
        env=None,
        network="bridge",
    )
    assert "--network=bridge" in argv
    assert "--network=none" not in argv


def test_run_in_sandbox_forwards_network(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        captured["argv"] = list(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "codeprobe.sandbox.runner._detect_engine", lambda: "docker"
    )
    monkeypatch.setattr("codeprobe.sandbox.runner.subprocess.run", fake_run)

    run_in_sandbox(["true"], {}, network="bridge")

    assert "--network=bridge" in captured["argv"]


def test_build_run_command_string_cmd_wrapped_in_sh_c() -> None:
    argv = _build_run_command(
        "docker",
        "echo hi | wc -l",
        {},
        allow_writes=False,
        image=UNIT_IMAGE,
        workdir=None,
        env=None,
    )
    assert argv[-3:] == ["sh", "-c", "echo hi | wc -l"]


def test_build_run_command_list_cmd_passes_through() -> None:
    argv = _build_run_command(
        "docker",
        ["ls", "-la", "/"],
        {},
        allow_writes=False,
        image=UNIT_IMAGE,
        workdir=None,
        env=None,
    )
    assert argv[-3:] == ["ls", "-la", "/"]


def test_build_run_command_includes_workdir_and_env() -> None:
    argv = _build_run_command(
        "docker",
        ["true"],
        {},
        allow_writes=False,
        image=UNIT_IMAGE,
        workdir="/workspace",
        env={"FOO": "bar"},
    )
    assert "-w" in argv
    assert "/workspace" in argv
    assert "-e" in argv
    assert "FOO=bar" in argv


@pytest.mark.parametrize(
    "bad_key",
    [
        "",
        "A=B",
        "NAME WITH SPACE",
        "NEWLINE\nKEY",
        "CARRIAGE\rKEY",
        "TAB\tKEY",
        "NUL\x00KEY",
        "1STARTS_WITH_DIGIT",
        "HAS-DASH",
    ],
)
def test_build_run_command_rejects_invalid_env_keys(bad_key: str) -> None:
    """Env var keys with '=', whitespace, newlines, or empty strings raise ValueError.

    Regression test for v0.6.0-batch-a finding: an unvalidated key like
    ``"A=B"`` silently produces ``-e A=B=VALUE`` which Docker parses as
    ``A -> B=VALUE``, which is not what the caller intended.
    """
    with pytest.raises(ValueError, match="Invalid env var key"):
        _build_run_command(
            "docker",
            ["true"],
            {},
            allow_writes=False,
            image=UNIT_IMAGE,
            workdir=None,
            env={bad_key: "value"},
        )


def test_build_run_command_accepts_valid_env_keys() -> None:
    """Sanity: well-formed env keys pass validation."""
    argv = _build_run_command(
        "docker",
        ["true"],
        {},
        allow_writes=False,
        image=UNIT_IMAGE,
        workdir=None,
        env={"FOO_BAR": "value", "BAZ123": "q"},
    )
    assert "FOO_BAR=value" in argv
    assert "BAZ123=q" in argv


def test_build_run_command_includes_container_name() -> None:
    argv = _build_run_command(
        "docker",
        ["true"],
        {},
        allow_writes=False,
        image=UNIT_IMAGE,
        workdir=None,
        env=None,
        container_name="codeprobe-sb-test",
    )
    assert argv[argv.index("--name") + 1] == "codeprobe-sb-test"


def test_build_run_command_omits_name_when_none() -> None:
    argv = _build_run_command(
        "docker",
        ["true"],
        {},
        allow_writes=False,
        image=UNIT_IMAGE,
        workdir=None,
        env=None,
    )
    assert "--name" not in argv


def test_build_run_command_includes_multiple_mounts() -> None:
    argv = _build_run_command(
        "docker",
        ["true"],
        {"/host/a": "/mnt/a", "/host/b": "/mnt/b"},
        allow_writes=False,
        image=UNIT_IMAGE,
        workdir=None,
        env=None,
    )
    assert "/host/a:/mnt/a:ro" in argv
    assert "/host/b:/mnt/b:ro" in argv


@pytest.mark.parametrize(
    "image",
    ["--privileged", "image:1.2.3", "registry.example.test/image:latest"],
)
def test_build_run_command_rejects_untrusted_image_reference(image: str) -> None:
    with pytest.raises(ValueError, match="image"):
        _build_run_command(
            "docker",
            ["true"],
            {},
            allow_writes=False,
            image=image,
            workdir=None,
            env=None,
        )


def test_image_available_rejects_untrusted_image_before_subprocess() -> None:
    with patch("codeprobe.sandbox.runner.subprocess.run") as run_mock:
        with pytest.raises(ValueError, match="image"):
            image_available("docker", "--privileged")

    run_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Runner error translation (mocked subprocess)
# ---------------------------------------------------------------------------


def _make_completed(stdout: str, stderr: str, returncode: int) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["docker"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_run_in_sandbox_success_returns_result() -> None:
    fake = _make_completed(stdout="hello\n", stderr="", returncode=0)
    with patch(
        "codeprobe.sandbox.runner._detect_engine", return_value="/usr/bin/docker"
    ), patch(
        "codeprobe.sandbox.runner.subprocess.run", return_value=fake
    ) as run_mock:
        result = run_in_sandbox(
            ["echo", "hello"],
            {"/tmp/src": "/workspace"},
            allow_writes=False,
        )
    assert isinstance(result, SandboxResult)
    assert result.stdout == "hello\n"
    assert result.exit_code == 0
    # Verify the argv that was passed to subprocess.run
    call_argv = run_mock.call_args.args[0]
    assert call_argv[0] == "/usr/bin/docker"
    assert "/tmp/src:/workspace:ro" in call_argv


def test_run_in_sandbox_ro_write_failure_raises_write_denied() -> None:
    fake = _make_completed(
        stdout="",
        stderr="touch: cannot touch '/mnt/x': Read-only file system\n",
        returncode=1,
    )
    with patch(
        "codeprobe.sandbox.runner._detect_engine", return_value="/usr/bin/docker"
    ), patch("codeprobe.sandbox.runner.subprocess.run", return_value=fake):
        with pytest.raises(SandboxWriteDeniedError):
            run_in_sandbox(
                ["touch", "/mnt/x"],
                {"/tmp/src": "/mnt"},
                allow_writes=False,
            )


def test_run_in_sandbox_write_denied_error_does_not_echo_stderr() -> None:
    secret = "sk-write-probe-secret"
    host_path = "/tmp/codeprobe-sensitive-worktree"
    fake = _make_completed(
        stdout="",
        stderr=(
            f"touch: cannot touch '{host_path}': Read-only file system; "
            f"token={secret}\n"
        ),
        returncode=1,
    )

    with patch(
        "codeprobe.sandbox.runner._detect_engine", return_value="/usr/bin/docker"
    ), patch("codeprobe.sandbox.runner.subprocess.run", return_value=fake):
        with pytest.raises(SandboxWriteDeniedError) as exc_info:
            run_in_sandbox(
                ["touch", host_path],
                {"/tmp/src": "/mnt"},
                allow_writes=False,
            )

    message = str(exc_info.value)
    assert "sandbox blocked write to read-only mount" in message
    assert secret not in message
    assert host_path not in message


def test_run_in_sandbox_ro_write_not_raised_when_allow_writes_true() -> None:
    # When the caller opted into writes, a "Read-only file system" stderr is
    # the agent's own concern — the runner should just report exit_code.
    fake = _make_completed(
        stdout="",
        stderr="touch: cannot touch '/mnt/x': Read-only file system\n",
        returncode=1,
    )
    with patch(
        "codeprobe.sandbox.runner._detect_engine", return_value="/usr/bin/docker"
    ), patch("codeprobe.sandbox.runner.subprocess.run", return_value=fake):
        result = run_in_sandbox(
            ["touch", "/mnt/x"],
            {"/tmp/src": "/mnt"},
            allow_writes=True,
        )
    assert result.exit_code == 1


def test_run_in_sandbox_non_write_failure_returns_result() -> None:
    # A generic non-zero exit (e.g. test failure, syntax error) must NOT be
    # promoted to SandboxWriteDeniedError — only ro-mount violations escalate.
    fake = _make_completed(stdout="", stderr="syntax error\n", returncode=2)
    with patch(
        "codeprobe.sandbox.runner._detect_engine", return_value="/usr/bin/docker"
    ), patch("codeprobe.sandbox.runner.subprocess.run", return_value=fake):
        result = run_in_sandbox(
            ["python", "-c", "invalid"],
            {"/tmp/src": "/mnt"},
            allow_writes=False,
        )
    assert result.exit_code == 2
    assert "syntax error" in result.stderr


def test_run_in_sandbox_timeout_translated_to_sandbox_error() -> None:
    def _raise_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="docker", timeout=0.1)

    with patch(
        "codeprobe.sandbox.runner._detect_engine", return_value="/usr/bin/docker"
    ), patch(
        "codeprobe.sandbox.runner.subprocess.run", side_effect=_raise_timeout
    ):
        with pytest.raises(SandboxError, match="timed out"):
            run_in_sandbox(
                ["sleep", "10"],
                {"/tmp": "/workspace"},
                timeout=0.1,
            )


def test_run_in_sandbox_debug_log_redacts_env_values_and_paths(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = _make_completed(stdout="", stderr="", returncode=0)
    secret = "sk-test-secret"
    command_secret = "command-token-that-must-not-leak"
    host_path = "/tmp/codeprobe-secret-host"

    with patch(
        "codeprobe.sandbox.runner._detect_engine", return_value="/usr/bin/docker"
    ), patch("codeprobe.sandbox.runner.subprocess.run", return_value=fake):
        caplog.set_level(logging.DEBUG, logger="codeprobe.sandbox.runner")
        run_in_sandbox(
            ["tool", "--token", command_secret],
            {host_path: "/workspace"},
            env={"ANTHROPIC_API_KEY": secret},
        )

    assert "sandbox run:" in caplog.text
    assert secret not in caplog.text
    assert command_secret not in caplog.text
    assert "--token" not in caplog.text
    assert host_path not in caplog.text
    assert "ANTHROPIC_API_KEY=<redacted>" in caplog.text
    assert "<redacted-command>" in caplog.text


def test_run_in_sandbox_timeout_error_redacts_env_values_and_paths() -> None:
    secret = "sk-timeout-secret"
    command_secret = "command-timeout-token-that-must-not-leak"
    host_path = "/tmp/codeprobe-secret-host"

    def _raise_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="docker", timeout=0.1)

    with patch(
        "codeprobe.sandbox.runner._detect_engine", return_value="/usr/bin/docker"
    ), patch(
        "codeprobe.sandbox.runner.subprocess.run", side_effect=_raise_timeout
    ), patch("codeprobe.sandbox.runner._force_remove_container", lambda *_: None):
        with pytest.raises(SandboxError, match="timed out") as exc_info:
            run_in_sandbox(
                ["tool", "--token", command_secret],
                {host_path: "/workspace"},
                timeout=0.1,
                env={"ANTHROPIC_API_KEY": secret},
            )

    message = str(exc_info.value)
    assert secret not in message
    assert command_secret not in message
    assert "--token" not in message
    assert host_path not in message
    assert "ANTHROPIC_API_KEY=<redacted>" in message
    assert "<redacted-command>" in message


def test_run_in_sandbox_generic_oserror_is_sanitized() -> None:
    def _raise_oserror(*_args, **_kwargs):
        raise PermissionError("secret path /tmp/codeprobe-secret-host")

    with patch(
        "codeprobe.sandbox.runner._detect_engine", return_value="/usr/bin/docker"
    ), patch("codeprobe.sandbox.runner.subprocess.run", side_effect=_raise_oserror):
        with pytest.raises(SandboxError) as exc_info:
            run_in_sandbox(["true"], {"/tmp/codeprobe-secret-host": "/workspace"})

    message = str(exc_info.value)
    assert "failed to launch" in message
    assert "secret" not in message
    assert "/tmp/codeprobe-secret-host" not in message


def test_run_in_sandbox_timeout_force_removes_container() -> None:
    """A client-side timeout kills the engine CLI only — the runner must
    ``rm -f`` the named container so a hung mined script cannot orphan it.
    """
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        calls.append(list(argv))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=0.1)
        if "inspect" in argv:
            # Container gone after removal.
            return _make_completed(stdout="", stderr="no such container", returncode=1)
        return _make_completed(stdout="", stderr="", returncode=0)

    with patch(
        "codeprobe.sandbox.runner._detect_engine", return_value="/usr/bin/docker"
    ), patch(
        "codeprobe.sandbox.runner.subprocess.run", side_effect=fake_run
    ), patch("codeprobe.sandbox.runner.time.sleep", lambda _s: None):
        with pytest.raises(SandboxError, match="timed out"):
            run_in_sandbox(
                ["sleep", "10"],
                {"/tmp": "/workspace"},
                timeout=0.1,
            )

    assert len(calls) == 3
    run_argv, rm_argv, inspect_argv = calls
    name = run_argv[run_argv.index("--name") + 1]
    assert name.startswith("codeprobe-sb-")
    assert rm_argv == ["/usr/bin/docker", "rm", "-f", name]
    assert inspect_argv == ["/usr/bin/docker", "container", "inspect", name]


def test_run_in_sandbox_timeout_rm_retries_create_race() -> None:
    """Killing the CLI mid-``run`` can race the daemon's create: the first
    ``rm -f`` finds nothing, then the container lands (Created, never
    started) and pins the image. Removal must retry until it is gone.
    """
    calls: list[list[str]] = []
    inspect_results = iter([0, 1])  # first: still exists; second: gone

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        calls.append(list(argv))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=0.1)
        if "inspect" in argv:
            return _make_completed(
                stdout="", stderr="", returncode=next(inspect_results)
            )
        return _make_completed(stdout="", stderr="", returncode=0)

    with patch(
        "codeprobe.sandbox.runner._detect_engine", return_value="/usr/bin/docker"
    ), patch(
        "codeprobe.sandbox.runner.subprocess.run", side_effect=fake_run
    ), patch("codeprobe.sandbox.runner.time.sleep", lambda _s: None):
        with pytest.raises(SandboxError, match="timed out"):
            run_in_sandbox(
                ["sleep", "10"],
                {"/tmp": "/workspace"},
                timeout=0.1,
            )

    # run, rm, inspect (exists), rm, inspect (gone)
    rm_calls = [argv for argv in calls if argv[1:3] == ["rm", "-f"]]
    assert len(rm_calls) == 2
    assert len(calls) == 5


def test_run_in_sandbox_timeout_rm_failure_still_raises_timeout() -> None:
    """A failing ``rm -f`` must not mask the original timeout error."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003
        calls.append(list(argv))
        raise subprocess.TimeoutExpired(cmd=argv, timeout=0.1)

    with patch(
        "codeprobe.sandbox.runner._detect_engine", return_value="/usr/bin/docker"
    ), patch(
        "codeprobe.sandbox.runner.subprocess.run", side_effect=fake_run
    ), patch("codeprobe.sandbox.runner.time.sleep", lambda _s: None):
        with pytest.raises(SandboxError, match="timed out"):
            run_in_sandbox(
                ["sleep", "10"],
                {"/tmp": "/workspace"},
                timeout=0.1,
            )

    # Both the run and the rm attempt happened; the rm timeout was absorbed.
    assert len(calls) == 2


def test_run_in_sandbox_missing_engine_raises() -> None:
    with patch(
        "codeprobe.sandbox.runner.shutil.which", return_value=None
    ):
        with pytest.raises(SandboxError, match="No container engine"):
            run_in_sandbox(["true"], {}, allow_writes=False)


def test_run_in_sandbox_prefers_docker_over_podman() -> None:
    # shutil.which returns docker path first; podman should never be queried
    def fake_which(name: str) -> str | None:
        return {"docker": "/usr/bin/docker", "podman": "/usr/bin/podman"}.get(name)

    fake = _make_completed(stdout="ok", stderr="", returncode=0)
    with patch(
        "codeprobe.sandbox.runner.shutil.which", side_effect=fake_which
    ), patch(
        "codeprobe.sandbox.runner.subprocess.run", return_value=fake
    ) as run_mock:
        run_in_sandbox(["true"], {}, allow_writes=False)
    argv = run_mock.call_args.args[0]
    assert argv[0] == "/usr/bin/docker"


def test_run_in_sandbox_falls_back_to_podman() -> None:
    def fake_which(name: str) -> str | None:
        return {"docker": None, "podman": "/usr/bin/podman"}.get(name)

    fake = _make_completed(stdout="ok", stderr="", returncode=0)
    with patch(
        "codeprobe.sandbox.runner.shutil.which", side_effect=fake_which
    ), patch(
        "codeprobe.sandbox.runner.subprocess.run", return_value=fake
    ) as run_mock:
        run_in_sandbox(["true"], {}, allow_writes=False)
    argv = run_mock.call_args.args[0]
    assert argv[0] == "/usr/bin/podman"


# ---------------------------------------------------------------------------
# sandbox_options Click decorator
# ---------------------------------------------------------------------------


def test_sandbox_options_default_false() -> None:
    captured: dict[str, object] = {}

    @click.command()
    @sandbox_options
    def cmd() -> None:
        ctx = click.get_current_context()
        captured["value"] = ctx.obj["allow_mutating_tools"]

    runner = CliRunner()
    result = runner.invoke(cmd, [])
    assert result.exit_code == 0, result.output
    assert captured["value"] is False


def test_sandbox_options_flag_sets_true() -> None:
    captured: dict[str, object] = {}

    @click.command()
    @sandbox_options
    def cmd() -> None:
        ctx = click.get_current_context()
        captured["value"] = ctx.obj["allow_mutating_tools"]

    runner = CliRunner()
    result = runner.invoke(cmd, ["--allow-mutating-tools"])
    assert result.exit_code == 0, result.output
    assert captured["value"] is True


def test_sandbox_options_help_mentions_flag() -> None:
    @click.command()
    @sandbox_options
    def cmd() -> None:
        pass

    runner = CliRunner()
    result = runner.invoke(cmd, ["--help"])
    assert result.exit_code == 0
    assert "--allow-mutating-tools" in result.output


# ---------------------------------------------------------------------------
# Docker-gated integration tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def docker_image() -> str:
    """Build the sandbox image once per module; skip if docker is unavailable."""
    if not HAS_DOCKER:
        pytest.skip("docker not installed")
    assert DOCKERFILE.is_file(), f"Dockerfile missing: {DOCKERFILE}"
    build = subprocess.run(
        [
            "docker",
            "build",
            "-f",
            str(DOCKERFILE),
            "-t",
            TEST_IMAGE_TAG,
            str(DOCKERFILE.parent),
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if build.returncode != 0:
        pytest.skip(f"docker build failed: {build.stderr[-500:]}")
    return TEST_IMAGE_TAG


@pytest.mark.skipif(not HAS_DOCKER, reason="docker not installed")
def test_docker_ls_does_not_leak_host_paths(
    docker_image: str, tmp_path: Path
) -> None:
    """`ls /` and `ls /home` inside the container must not reveal host paths.

    The slim base image contains an empty ``/home`` directory — that is
    expected and not a leak. The assertion is that no host-side
    ``/home/<user>`` entries (or contents of the invoking user's homedir)
    bleed through the container boundary.
    """
    # Mount a tmpdir so we exercise a real mount but still verify containment.
    (tmp_path / "marker.txt").write_text("hi")
    _assert_container_root_hides_host(docker_image, tmp_path)
    _assert_container_home_hides_host(docker_image, tmp_path)
    _assert_host_home_path_absent(docker_image, tmp_path)


def _assert_container_root_hides_host(docker_image: str, tmp_path: Path) -> None:
    result_root = run_in_sandbox(
        ["ls", "/"],
        {str(tmp_path): "/workspace"},
        allow_writes=False,
        image=docker_image,
        timeout=60.0,
    )
    assert result_root.exit_code == 0, result_root.stderr
    assert "workspace" in result_root.stdout
    assert "/home/" not in result_root.stdout


def _assert_container_home_hides_host(docker_image: str, tmp_path: Path) -> None:
    result_home = run_in_sandbox(
        ["ls", "-la", "/home"],
        {str(tmp_path): "/workspace"},
        allow_writes=False,
        image=docker_image,
        timeout=60.0,
    )
    assert result_home.exit_code == 0, result_home.stderr
    # The test runner's real host homedir basename must not leak in.
    host_home = Path.home().name
    if host_home:
        assert host_home not in result_home.stdout, (
            f"container leaked host home entry {host_home!r}: "
            f"{result_home.stdout!r}"
        )


def _assert_host_home_path_absent(docker_image: str, tmp_path: Path) -> None:
    host_home_path = str(Path.home())
    result_probe = run_in_sandbox(
        ["ls", host_home_path],
        {str(tmp_path): "/workspace"},
        allow_writes=False,
        image=docker_image,
        timeout=60.0,
    )
    assert result_probe.exit_code != 0, (
        f"host path {host_home_path!r} is visible inside the container: "
        f"{result_probe.stdout!r}"
    )


@pytest.mark.skipif(not HAS_DOCKER, reason="docker not installed")
def test_docker_write_to_ro_mount_raises_write_denied(
    docker_image: str, tmp_path: Path
) -> None:
    """A write into a :ro bind mount must raise SandboxWriteDeniedError."""
    (tmp_path / "existing.txt").write_text("hi")
    with pytest.raises(SandboxWriteDeniedError):
        run_in_sandbox(
            ["touch", "/workspace/newfile.txt"],
            {str(tmp_path): "/workspace"},
            allow_writes=False,
            image=docker_image,
            timeout=60.0,
        )
    # Verify nothing was actually written to the host
    assert not (tmp_path / "newfile.txt").exists()


@pytest.mark.skipif(not HAS_DOCKER, reason="docker not installed")
def test_docker_write_allowed_when_rw(
    docker_image: str, tmp_path: Path
) -> None:
    """With allow_writes=True, the container can mutate the bind mount."""
    result = run_in_sandbox(
        ["touch", "/workspace/newfile.txt"],
        {str(tmp_path): "/workspace"},
        allow_writes=True,
        image=docker_image,
        timeout=60.0,
    )
    assert result.exit_code == 0, result.stderr
    assert (tmp_path / "newfile.txt").exists()
