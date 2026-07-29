"""Unit tests for agent containerization (codeprobe-f7rl.5).

Covers the pure argv wrapper (``containerize_argv``) and the
``BaseAdapter.run`` integration: a ``container`` plan wraps the adapter
argv in ``<engine> run``; sandboxed / host-consented / plan-less callers
keep the exact ``build_command`` argv; a client-side timeout triggers a
best-effort ``<engine> rm -f <name>``.
"""

from __future__ import annotations

import os
import subprocess
from importlib.metadata import version as package_version
from pathlib import Path

import pytest

from codeprobe.adapters._base import BaseAdapter
from codeprobe.adapters.protocol import AgentConfig
from codeprobe.core import containment
from codeprobe.sandbox.agent_container import containerize_argv

ENGINE = "/usr/bin/docker"
IMAGE = f"registry.example.test/codeprobe-agent:{package_version('codeprobe')}"


# ---------------------------------------------------------------------------
# containerize_argv (pure argv construction)
# ---------------------------------------------------------------------------


class TestContainerizeArgv:
    def test_full_shape_and_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        argv = containerize_argv(
            ["/host/bin/claude", "-p", "hi", "--verbose"],
            engine=ENGINE,
            workspace=Path("/work/slot0"),
            config_dir=Path("/cfg/slot0"),
            mcp_tmpfile="/tmp/codeprobe-mcp-x.json",
            env_keys=["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"],
            image=IMAGE,
            name="codeprobe-agent-abc",
        )

        assert argv == _expected_full_container_argv()

    def test_optional_mounts_omitted_when_absent(self) -> None:
        argv = containerize_argv(
            ["/host/bin/claude", "-p", "hi"],
            engine=ENGINE,
            workspace=Path("/work/slot0"),
            config_dir=None,
            mcp_tmpfile=None,
            env_keys=[],
            image=IMAGE,
            name="codeprobe-agent-abc",
        )

        mounts = [argv[i + 1] for i, tok in enumerate(argv) if tok == "-v"]
        assert mounts == ["/work/slot0:/work/slot0:rw"]
        assert "ANTHROPIC_API_KEY" not in argv

    @pytest.mark.parametrize("engine", ["/usr/bin/docker", "/usr/bin/podman"])
    def test_runtime_limits_are_docker_and_podman_compatible(
        self, engine: str
    ) -> None:
        argv = containerize_argv(
            ["/host/bin/claude", "-p", "hi"],
            engine=engine,
            workspace=Path("/work/slot0"),
            config_dir=None,
            mcp_tmpfile=None,
            env_keys=[],
            image=IMAGE,
            name="codeprobe-agent-abc",
        )

        assert argv[0] == engine
        assert "--pull=never" in argv
        assert "--cpus=2" in argv
        assert "--memory=4g" in argv
        assert "--memory-swap=4g" in argv
        assert "--pids-limit=256" in argv
        assert "--read-only" in argv

    def test_env_keys_filtered_against_explicit_mapping(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Session-env-only keys survive when the effective env is passed."""
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

        argv = containerize_argv(
            ["/host/bin/claude"],
            engine=ENGINE,
            workspace=Path("/work/slot0"),
            config_dir=None,
            mcp_tmpfile=None,
            env_keys=["CLAUDE_CONFIG_DIR", "GITHUB_TOKEN"],
            image=IMAGE,
            name="codeprobe-agent-abc",
            env={"CLAUDE_CONFIG_DIR": "/cfg/slot0"},
        )

        env_pairs = [argv[index : index + 2] for index, token in enumerate(argv) if token == "-e"]
        assert ["-e", "CLAUDE_CONFIG_DIR"] in env_pairs
        assert "GITHUB_TOKEN" not in argv

    def test_empty_cmd_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            containerize_argv(
                [],
                engine=ENGINE,
                workspace=Path("/work"),
                config_dir=None,
                mcp_tmpfile=None,
                env_keys=[],
                image=IMAGE,
                name="codeprobe-agent-abc",
            )

    @pytest.mark.parametrize(
        "image",
        ["--privileged", "codeprobe-agent:1.2.3", "registry.example.test/agent:latest"],
    )
    def test_invalid_image_reference_rejected(self, image: str) -> None:
        with pytest.raises(ValueError, match="image"):
            containerize_argv(
                ["/host/bin/claude"],
                engine=ENGINE,
                workspace=Path("/work"),
                config_dir=None,
                mcp_tmpfile=None,
                env_keys=[],
                image=image,
                name="codeprobe-agent-abc",
            )


def _expected_full_container_argv() -> list[str]:
    expected = [
        ENGINE,
        "run",
        "--rm",
        "--pull=never",
        "--network=bridge",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--cpus=2",
        "--memory=4g",
        "--memory-swap=4g",
        "--pids-limit=256",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=128m,mode=1777",
        "-e",
        "HOME=/tmp",
        "-e",
        "TMPDIR=/tmp",
    ]
    if hasattr(os, "getuid") and hasattr(os, "getgid"):
        expected += ["--user", f"{os.getuid()}:{os.getgid()}"]
    expected += _expected_full_container_tail()
    return expected


def _expected_full_container_tail() -> list[str]:
    return [
        "--name",
        "codeprobe-agent-abc",
        "-v",
        "/work/slot0:/work/slot0:rw",
        "-v",
        "/cfg/slot0:/cfg/slot0:rw",
        "-v",
        "/tmp/codeprobe-mcp-x.json:/tmp/codeprobe-mcp-x.json:ro",
        "-e",
        "ANTHROPIC_API_KEY",
        "-w",
        "/work/slot0",
        IMAGE,
        "claude",
        "-p",
        "hi",
        "--verbose",
    ]


# ---------------------------------------------------------------------------
# BaseAdapter.run integration
# ---------------------------------------------------------------------------


class _FakeAdapter(BaseAdapter):
    _binary_name = "fake-agent"
    _install_hint = "install fake-agent"

    def build_command(self, prompt: str, config: AgentConfig) -> list[str]:
        return ["/host/bin/fake-agent", "-p", prompt]


@pytest.fixture()
def capture_run(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Patch subprocess.run inside the adapter module; return captured argvs."""
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("codeprobe.adapters._base.subprocess.run", fake_run)
    return calls


@pytest.fixture()
def configured_agent_image(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("CODEPROBE_AGENT_IMAGE", IMAGE)
    return IMAGE


class TestRunContainerization:
    def test_container_plan_wraps_argv(
        self,
        capture_run: list[list[str]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        configured_agent_image: str,
    ) -> None:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        containment.set_active_plan(
            containment.ContainmentPlan(mode="container", engine=ENGINE)
        )

        output = _FakeAdapter().run("hello", AgentConfig(cwd=str(tmp_path)))

        assert output.exit_code == 0
        (argv,) = capture_run
        assert argv[:3] == [ENGINE, "run", "--rm"]
        assert "--pull=never" in argv
        assert "--network=bridge" in argv
        name = argv[argv.index("--name") + 1]
        assert name.startswith("codeprobe-agent-")
        assert f"{tmp_path}:{tmp_path}:rw" in argv
        assert argv[argv.index(configured_agent_image) :] == [
            configured_agent_image,
            "fake-agent",
            "-p",
            "hello",
        ]

    def test_container_plan_mounts_session_config_dir(
        self,
        capture_run: list[list[str]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        configured_agent_image: str,
    ) -> None:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        containment.set_active_plan(
            containment.ContainmentPlan(mode="container", engine=ENGINE)
        )

        _FakeAdapter().run(
            "hello",
            AgentConfig(cwd=str(tmp_path)),
            session_env={"CLAUDE_CONFIG_DIR": "/cfg/slot3"},
        )

        (argv,) = capture_run
        assert "/cfg/slot3:/cfg/slot3:rw" in argv
        assert "CLAUDE_CONFIG_DIR" in argv  # -e passthrough from session env

    def test_host_global_config_dir_never_mounted(
        self,
        capture_run: list[list[str]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        configured_agent_image: str,
    ) -> None:
        """Without a session config dir the container gets NO config-dir
        mount and no -e CLAUDE_CONFIG_DIR, even when the host-global env
        var is set — mounting the user's real config dir rw would hand the
        agent live credential/settings state (verification fix)."""
        host_cfg = tmp_path / "host-claude"
        host_cfg.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(host_cfg))
        containment.set_active_plan(
            containment.ContainmentPlan(mode="container", engine=ENGINE)
        )

        _FakeAdapter().run("hello", AgentConfig(cwd=str(tmp_path)))

        (argv,) = capture_run
        mounts = [argv[i + 1] for i, tok in enumerate(argv) if tok == "-v"]
        assert mounts == [f"{tmp_path}:{tmp_path}:rw"]
        assert "CLAUDE_CONFIG_DIR" not in argv

    def test_session_env_without_config_dir_omits_mount(
        self,
        capture_run: list[list[str]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        configured_agent_image: str,
    ) -> None:
        """A session env that carries no CLAUDE_CONFIG_DIR must not fall
        back to the host-global one."""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "host-claude"))
        containment.set_active_plan(
            containment.ContainmentPlan(mode="container", engine=ENGINE)
        )

        _FakeAdapter().run(
            "hello",
            AgentConfig(cwd=str(tmp_path)),
            session_env={"ANTHROPIC_API_KEY": "sk-session"},
        )

        (argv,) = capture_run
        mounts = [argv[i + 1] for i, tok in enumerate(argv) if tok == "-v"]
        assert mounts == [f"{tmp_path}:{tmp_path}:rw"]
        assert "CLAUDE_CONFIG_DIR" not in argv

    def test_container_plan_mounts_private_ca_paths(
        self,
        capture_run: list[list[str]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        configured_agent_image: str,
    ) -> None:
        ca_file = tmp_path / "corp-ca.pem"
        ca_file.write_text("certificate", encoding="utf-8")
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        same_name_ca = other_dir / "corp-ca.pem"
        same_name_ca.write_text("other certificate", encoding="utf-8")
        ca_dir = tmp_path / "ca-dir"
        ca_dir.mkdir()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("SSL_CERT_FILE", str(ca_file))
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(ca_file))
        monkeypatch.setenv("CURL_CA_BUNDLE", str(same_name_ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(ca_dir))
        containment.set_active_plan(
            containment.ContainmentPlan(mode="container", engine=ENGINE)
        )

        _FakeAdapter().run("hello", AgentConfig(cwd=str(tmp_path)))

        (argv,) = capture_run
        mounts = [argv[i + 1] for i, tok in enumerate(argv) if tok == "-v"]
        env_args = [argv[i + 1] for i, tok in enumerate(argv) if tok == "-e"]
        assert f"{tmp_path}:{tmp_path}:rw" in mounts
        assert (
            f"{ca_file.resolve()}:/etc/codeprobe/ca/00-corp-ca.pem:ro"
            in mounts
        )
        assert (
            f"{same_name_ca.resolve()}:/etc/codeprobe/ca/01-corp-ca.pem:ro"
            in mounts
        )
        assert f"{ca_dir.resolve()}:/etc/codeprobe/ca/02-ca-dir:ro" in mounts
        assert (
            mounts.count(
                f"{ca_file.resolve()}:/etc/codeprobe/ca/00-corp-ca.pem:ro"
            )
            == 1
        )
        assert "SSL_CERT_FILE=/etc/codeprobe/ca/00-corp-ca.pem" in env_args
        assert "REQUESTS_CA_BUNDLE=/etc/codeprobe/ca/00-corp-ca.pem" in env_args
        assert "CURL_CA_BUNDLE=/etc/codeprobe/ca/01-corp-ca.pem" in env_args
        assert "SSL_CERT_DIR=/etc/codeprobe/ca/02-ca-dir" in env_args
        assert "ANTHROPIC_API_KEY" in env_args
        assert not any(str(tmp_path) in arg for arg in env_args)

    @pytest.mark.parametrize(
        "plan",
        [
            None,
            containment.ContainmentPlan(mode="sandboxed"),
            containment.ContainmentPlan(mode="host-consented"),
        ],
    )
    def test_non_container_plans_keep_build_command_argv(
        self,
        capture_run: list[list[str]],
        tmp_path: Path,
        plan: containment.ContainmentPlan | None,
    ) -> None:
        if plan is not None:
            containment.set_active_plan(plan)

        adapter = _FakeAdapter()
        adapter.run("hello", AgentConfig(cwd=str(tmp_path)))

        (argv,) = capture_run
        assert argv == adapter.build_command("hello", AgentConfig(cwd=str(tmp_path)))

    def test_timeout_removes_container(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        configured_agent_image: str,
    ) -> None:
        calls: list[list[str]] = []

        def fake_run(
            argv: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append(list(argv))
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=1)
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="", stderr=""
            )

        monkeypatch.setattr("codeprobe.adapters._base.subprocess.run", fake_run)
        containment.set_active_plan(
            containment.ContainmentPlan(mode="container", engine=ENGINE)
        )

        output = _FakeAdapter().run(
            "hello", AgentConfig(cwd=str(tmp_path), timeout_seconds=1)
        )

        assert output.exit_code == -1
        assert output.error is not None and "timed out" in output.error
        assert len(calls) == 2
        run_argv, rm_argv = calls
        name = run_argv[run_argv.index("--name") + 1]
        assert rm_argv == [ENGINE, "rm", "-f", name]

    def test_timeout_without_container_plan_skips_rm(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[list[str]] = []

        def fake_run(
            argv: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append(list(argv))
            raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

        monkeypatch.setattr("codeprobe.adapters._base.subprocess.run", fake_run)

        output = _FakeAdapter().run(
            "hello", AgentConfig(cwd=str(tmp_path), timeout_seconds=1)
        )

        assert output.exit_code == -1
        assert len(calls) == 1
