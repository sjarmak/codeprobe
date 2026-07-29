"""Tests for codeprobe.sandbox.runner and codeprobe.cli._sandbox.

Unit tests (always run) exercise the argv construction and error
translation via mocked subprocess. Docker-gated integration tests are
marked with :func:`pytest.mark.skipif` so the suite passes cleanly on
machines without docker installed.
"""

from __future__ import annotations

import shutil
import subprocess
from importlib.metadata import version as package_version
from pathlib import Path
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from codeprobe.cli._sandbox import sandbox_options
from codeprobe.sandbox import runner as sandbox_runner
from codeprobe.sandbox.runner import (
    SandboxError,
    SandboxResult,
    SandboxWriteDeniedError,
    _build_run_command,
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
TEST_IMAGE_TAG = "codeprobe-sandbox:sg-only-test"

_IMAGE_ENV_KEYS = (
    "CODEPROBE_AGENT_IMAGE",
    "CODEPROBE_SCORING_IMAGE",
    "CODEPROBE_IMAGE_REGISTRY",
    "CODEPROBE_IMAGE_NAMESPACE",
    "CODEPROBE_IMAGE_VERSION",
)


# ---------------------------------------------------------------------------
# Runtime image references (pure config)
# ---------------------------------------------------------------------------


def _clear_image_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _IMAGE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_agent_and_scoring_images_track_installed_package_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_image_env(monkeypatch)
    release_version = package_version("codeprobe")

    assert sandbox_runner.agent_image_reference() == f"codeprobe-agent:{release_version}"
    assert sandbox_runner.scoring_image_reference() == f"codeprobe-scoring:{release_version}"


def test_image_reference_composes_registry_namespace_and_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_image_env(monkeypatch)
    monkeypatch.setenv("CODEPROBE_IMAGE_REGISTRY", "registry.example.test")
    monkeypatch.setenv("CODEPROBE_IMAGE_NAMESPACE", "platform/codeprobe")
    monkeypatch.setenv("CODEPROBE_IMAGE_VERSION", "1.2.3")

    assert (
        sandbox_runner.agent_image_reference()
        == "registry.example.test/platform/codeprobe/codeprobe-agent:1.2.3"
    )
    assert (
        sandbox_runner.scoring_image_reference()
        == "registry.example.test/platform/codeprobe/codeprobe-scoring:1.2.3"
    )


def test_exact_agent_and_scoring_image_overrides_win(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_image_env(monkeypatch)
    monkeypatch.setenv("CODEPROBE_IMAGE_REGISTRY", "registry.example.test")
    agent_digest = "a" * 64
    scoring_digest = "b" * 64
    monkeypatch.setenv(
        "CODEPROBE_AGENT_IMAGE", f"mirror.example/agent@sha256:{agent_digest}"
    )
    monkeypatch.setenv(
        "CODEPROBE_SCORING_IMAGE",
        f"mirror.example/scoring@sha256:{scoring_digest}",
    )

    assert (
        sandbox_runner.agent_image_reference()
        == f"mirror.example/agent@sha256:{agent_digest}"
    )
    assert (
        sandbox_runner.scoring_image_reference()
        == f"mirror.example/scoring@sha256:{scoring_digest}"
    )


def test_exact_image_override_accepts_ipv6_tag_and_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_image_env(monkeypatch)
    digest = "a" * 64
    reference = f"[2001:db8::1]:5000/platform/codeprobe-agent:1.2.3@sha256:{digest}"
    monkeypatch.setenv("CODEPROBE_AGENT_IMAGE", reference)

    assert sandbox_runner.agent_image_reference() == reference


def test_empty_image_override_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_image_env(monkeypatch)
    monkeypatch.setenv("CODEPROBE_AGENT_IMAGE", " ")

    with pytest.raises(ValueError, match="CODEPROBE_AGENT_IMAGE"):
        sandbox_runner.agent_image_reference()


@pytest.mark.parametrize(
    ("env_key", "value", "message"),
    [
        ("CODEPROBE_AGENT_IMAGE", "mirror.example/agent", "explicit tag or digest"),
        ("CODEPROBE_AGENT_IMAGE", "mirror.example/agent:latest", "latest"),
        ("CODEPROBE_AGENT_IMAGE", "https://mirror.example/agent:1.2.3", "not a URL"),
        ("CODEPROBE_AGENT_IMAGE", "mirror.example/agent@sha256:abc123", "sha256"),
        ("CODEPROBE_AGENT_IMAGE", "registry..example/team/agent:1.2.3", "invalid image reference"),
        ("CODEPROBE_AGENT_IMAGE", "registry.-example/team/agent:1.2.3", "invalid image reference"),
        ("CODEPROBE_IMAGE_REGISTRY", "REGISTRY.example.test", "registry host"),
        ("CODEPROBE_IMAGE_NAMESPACE", "platform//codeprobe", "repository path"),
        ("CODEPROBE_IMAGE_VERSION", "latest", "latest"),
    ],
)
def test_invalid_image_reference_parts_fail_fast(
    monkeypatch: pytest.MonkeyPatch,
    env_key: str,
    value: str,
    message: str,
) -> None:
    _clear_image_env(monkeypatch)
    monkeypatch.setenv(env_key, value)

    with pytest.raises(ValueError, match=message):
        sandbox_runner.agent_image_reference()


# ---------------------------------------------------------------------------
# Argv construction (pure, no subprocess)
# ---------------------------------------------------------------------------


def test_build_run_command_uses_ro_mode_by_default() -> None:
    argv = _build_run_command(
        "docker",
        ["echo", "hi"],
        {"/host/src": "/workspace"},
        allow_writes=False,
        image="codeprobe-sandbox:sg-only",
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
        image="codeprobe-sandbox:sg-only",
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
        image="codeprobe-sandbox:sg-only",
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
        image="codeprobe-sandbox:sg-only",
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
        image="img",
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
        image="img",
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
            image="img",
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
        image="img",
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
        image="img",
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
        image="img",
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
        image="img",
        workdir=None,
        env=None,
    )
    assert "/host/a:/mnt/a:ro" in argv
    assert "/host/b:/mnt/b:ro" in argv


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

    # 1. `ls /` should show rootfs basenames only — no absolute host paths
    result_root = run_in_sandbox(
        ["ls", "/"],
        {str(tmp_path): "/workspace"},
        allow_writes=False,
        image=docker_image,
        timeout=60.0,
    )
    assert result_root.exit_code == 0, result_root.stderr
    assert "workspace" in result_root.stdout
    # No absolute host paths should appear anywhere in stdout
    assert "/home/" not in result_root.stdout

    # 2. `ls /home` should be empty (or at most contain entries that exist
    #    in the base image), never anything under the invoking user's
    #    host homedir.
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

    # 3. Direct attempt to access the host's real homedir path inside the
    #    container must fail — the path simply does not exist there.
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
