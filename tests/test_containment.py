"""Unit tests for codeprobe.core.containment (codeprobe-f7rl.3, .5).

``resolve_containment`` is the single policy decision for where a run's
agent and mined scripts may execute: sandboxed (container detected or
user-set ``CODEPROBE_SANDBOX=1``), host-consented (``--uncontained``),
container (engine on PATH plus the agent image built), or a hard
``UNCONTAINED_REFUSED`` refusal carrying the full disclosure.
"""

from __future__ import annotations

import dataclasses
from importlib.metadata import version as package_version
from unittest.mock import Mock

import pytest

from codeprobe.cli.errors import PrescriptiveError
from codeprobe.core import containment
from codeprobe.sandbox import runner as container_runner

LOCAL_AGENT_IMAGE = f"codeprobe-agent:{package_version('codeprobe')}"
AGENT_IMAGE = (
    f"registry.example.test/platform/codeprobe/codeprobe-agent:"
    f"{package_version('codeprobe')}"
)


@pytest.fixture(autouse=True)
def _fresh_plan_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the module-level active-plan slot per test."""
    monkeypatch.setattr(containment, "_active_plan", None)
    for name in (
        container_runner.AGENT_IMAGE_ENV,
        container_runner.IMAGE_REGISTRY_ENV,
        container_runner.IMAGE_NAMESPACE_ENV,
        container_runner.IMAGE_VERSION_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


class TestResolveContainment:
    def test_sandboxed_host_resolves_sandboxed_without_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "codeprobe.core.sandbox.is_sandboxed", lambda: True
        )

        plan = containment.resolve_containment(uncontained=False)

        assert plan == containment.ContainmentPlan(mode="sandboxed")

    def test_uncontained_flag_resolves_host_consented(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "codeprobe.core.sandbox.is_sandboxed", lambda: False
        )
        monkeypatch.delenv("CODEPROBE_SANDBOX", raising=False)

        plan = containment.resolve_containment(uncontained=True)

        assert plan == containment.ContainmentPlan(mode="host-consented")

    def test_bare_host_without_flag_refuses_with_disclosure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "codeprobe.core.sandbox.is_sandboxed", lambda: False
        )
        monkeypatch.delenv("CODEPROBE_SANDBOX", raising=False)
        # Hermetic: a docker host with the agent image built would
        # otherwise resolve a container plan instead of refusing.
        monkeypatch.setattr(
            "codeprobe.sandbox.runner.detect_engine", lambda: None
        )

        with pytest.raises(PrescriptiveError) as exc_info:
            containment.resolve_containment(uncontained=False)

        err = exc_info.value
        assert err.code == "UNCONTAINED_REFUSED"
        assert err.next_try_flag == "--uncontained"
        assert err.next_try_value == ""
        assert "--dangerously-skip-permissions" in err.message
        assert "third-party test/verifier" in err.message
        assert "filesystem, credential, and network access" in err.message
        assert "container" in err.message

    def test_user_set_env_var_counts_as_sandboxed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CODEPROBE_SANDBOX survives as a USER-set consent signal."""
        monkeypatch.setenv("CODEPROBE_SANDBOX", "1")

        plan = containment.resolve_containment(uncontained=False)

        assert plan.mode == "sandboxed"


class TestContainerMode:
    """Third branch (codeprobe-f7rl.5): auto-container on engine + image."""

    @pytest.fixture(autouse=True)
    def _bare_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "codeprobe.core.sandbox.is_sandboxed", lambda: False
        )
        monkeypatch.delenv("CODEPROBE_SANDBOX", raising=False)

    def test_engine_and_agent_image_resolve_container_without_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, str] = {}
        monkeypatch.setenv(container_runner.AGENT_IMAGE_ENV, AGENT_IMAGE)

        def fake_image_available(engine: str, image: str) -> bool:
            seen["engine"] = engine
            seen["image"] = image
            return True

        monkeypatch.setattr(
            "codeprobe.sandbox.runner.detect_engine",
            lambda: "/usr/bin/docker",
        )
        monkeypatch.setattr(
            "codeprobe.sandbox.runner.image_available", fake_image_available
        )

        plan = containment.resolve_containment(uncontained=False)

        assert plan.mode == "container"
        assert plan.engine == "/usr/bin/docker"
        assert seen == {
            "engine": "/usr/bin/docker",
            "image": AGENT_IMAGE,
        }

    def test_engine_without_agent_image_config_refuses_before_inspect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        image_available = Mock(return_value=False)
        monkeypatch.setattr(
            "codeprobe.sandbox.runner.detect_engine",
            lambda: "/usr/bin/docker",
        )
        monkeypatch.setattr(
            "codeprobe.sandbox.runner.image_available", image_available
        )

        with pytest.raises(PrescriptiveError) as exc_info:
            containment.resolve_containment(uncontained=False)

        err = exc_info.value
        assert err.code == "UNCONTAINED_REFUSED"
        assert "agent image is not configured" in err.message
        assert "CODEPROBE_AGENT_IMAGE" in err.message
        assert f"-t {LOCAL_AGENT_IMAGE} ." in err.message
        assert not image_available.called

    def test_engine_with_missing_agent_image_refuses_with_build_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(container_runner.AGENT_IMAGE_ENV, AGENT_IMAGE)
        monkeypatch.setattr(
            "codeprobe.sandbox.runner.detect_engine",
            lambda: "/usr/bin/docker",
        )
        monkeypatch.setattr(
            "codeprobe.sandbox.runner.image_available",
            lambda engine, image: False,
        )

        with pytest.raises(PrescriptiveError) as exc_info:
            containment.resolve_containment(uncontained=False)

        err = exc_info.value
        assert err.code == "UNCONTAINED_REFUSED"
        assert AGENT_IMAGE in err.message
        assert (
            "docker build -f src/codeprobe/sandbox/Dockerfile.agent "
            f"-t {LOCAL_AGENT_IMAGE} ." in err.message
        )

    def test_invalid_agent_image_config_refuses_prescriptively(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        image_available = Mock(return_value=True)
        monkeypatch.setenv(container_runner.IMAGE_VERSION_ENV, "invalid tag")
        monkeypatch.setattr(
            "codeprobe.sandbox.runner.detect_engine",
            lambda: "/usr/bin/docker",
        )
        monkeypatch.setattr(
            "codeprobe.sandbox.runner.image_available", image_available
        )

        with pytest.raises(PrescriptiveError) as exc_info:
            containment.resolve_containment(uncontained=False)

        err = exc_info.value
        assert err.code == "UNCONTAINED_REFUSED"
        assert "CODEPROBE_IMAGE_VERSION" in err.message
        assert "CODEPROBE_AGENT_IMAGE" in err.message
        assert f"-t {LOCAL_AGENT_IMAGE} ." in err.message
        assert not image_available.called

    def test_uncontained_flag_wins_over_container_detection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit --uncontained consent means host execution, engine or not."""
        monkeypatch.setattr(
            "codeprobe.sandbox.runner.detect_engine",
            lambda: "/usr/bin/docker",
        )
        monkeypatch.setattr(
            "codeprobe.sandbox.runner.image_available",
            lambda engine, image: True,
        )

        plan = containment.resolve_containment(uncontained=True)

        assert plan.mode == "host-consented"
        assert plan.engine is None


class TestActivePlan:
    def test_active_plan_defaults_to_none(self) -> None:
        assert containment.active_plan() is None

    def test_set_active_plan_roundtrip(self) -> None:
        plan = containment.ContainmentPlan(mode="host-consented")

        containment.set_active_plan(plan)

        assert containment.active_plan() is plan

    def test_plan_is_immutable(self) -> None:
        plan = containment.ContainmentPlan(mode="sandboxed")
        with pytest.raises(dataclasses.FrozenInstanceError):
            plan.mode = "host-consented"  # type: ignore[misc]
