"""R1: widened instruction_mcp.md trigger + capability-based rendering."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from codeprobe.mcp.capabilities import Capability
from codeprobe.mining.writer import (
    _mcp_variant_triggered,
    _render_mcp_section,
    write_task_dir,
)
from codeprobe.models.task import Task, TaskMetadata, TaskVerification


def _make_task(
    *,
    task_id: str = "t00001",
    task_type: str = "sdlc_code_change",
    org_scale: bool = False,
    sg_repo: str = "",
    issue_title: str = "Example",
    issue_body: str = "Do the thing",
    verification: TaskVerification | None = None,
) -> Task:
    return Task(
        id=task_id,
        repo="myrepo",
        metadata=TaskMetadata(
            name=f"merge-{task_id}",
            difficulty="medium",
            description="Example description",
            language="python",
            task_type=task_type,
            org_scale=org_scale,
            sg_repo=sg_repo,
            issue_title=issue_title,
            issue_body=issue_body,
        ),
        verification=verification
        or TaskVerification(
            type="test_script",
            command="pytest tests/test_thing.py",
            reward_type="binary",
        ),
    )


# ---------------------------------------------------------------------------
# Trigger predicate
# ---------------------------------------------------------------------------


class TestMCPVariantTrigger:
    """`_mcp_variant_triggered` gates writing instruction_mcp.md."""

    def test_mcp_tool_usage_triggers(self) -> None:
        assert _mcp_variant_triggered(_make_task(task_type="mcp_tool_usage"))

    def test_org_scale_cross_repo_triggers(self) -> None:
        assert _mcp_variant_triggered(_make_task(task_type="org_scale_cross_repo"))

    def test_org_scale_flag_triggers(self) -> None:
        assert _mcp_variant_triggered(_make_task(org_scale=True))

    def test_sg_repo_triggers(self) -> None:
        assert _mcp_variant_triggered(_make_task(sg_repo="github.com/foo/bar"))

    def test_plain_sdlc_does_not_trigger(self) -> None:
        assert not _mcp_variant_triggered(_make_task())


# ---------------------------------------------------------------------------
# write_task_dir — integration: instruction_mcp.md is emitted for the
# widened trigger set.
# ---------------------------------------------------------------------------


class TestWriteTaskDirEmitsVariant:
    """write_task_dir emits instruction_mcp.md under the widened R1 trigger."""

    def test_sdlc_org_scale_produces_mcp_variant(self, tmp_path: Path) -> None:
        """sdlc_code_change + org_scale=True must emit instruction_mcp.md."""
        task = _make_task(task_type="sdlc_code_change", org_scale=True)
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "myrepo"

        result_path = write_task_dir(task, base_dir, repo_path)

        assert (result_path / "instruction.md").is_file()
        assert (result_path / "instruction_mcp.md").is_file()

    def test_mcp_tool_usage_still_emits_variant(self, tmp_path: Path) -> None:
        task = _make_task(task_type="mcp_tool_usage")
        result_path = write_task_dir(task, tmp_path / "tasks", tmp_path / "myrepo")
        assert (result_path / "instruction_mcp.md").is_file()

    def test_org_scale_cross_repo_emits_variant(self, tmp_path: Path) -> None:
        task = _make_task(task_type="org_scale_cross_repo")
        result_path = write_task_dir(task, tmp_path / "tasks", tmp_path / "myrepo")
        assert (result_path / "instruction_mcp.md").is_file()

    def test_sg_repo_emits_variant(self, tmp_path: Path) -> None:
        task = _make_task(sg_repo="github.com/foo/bar")
        result_path = write_task_dir(task, tmp_path / "tasks", tmp_path / "myrepo")
        assert (result_path / "instruction_mcp.md").is_file()

    def test_plain_sdlc_does_not_emit_variant(self, tmp_path: Path) -> None:
        task = _make_task()
        result_path = write_task_dir(task, tmp_path / "tasks", tmp_path / "myrepo")
        assert (result_path / "instruction.md").is_file()
        assert not (result_path / "instruction_mcp.md").exists()


# ---------------------------------------------------------------------------
# Variant body is rendered from codeprobe.mcp.capabilities — NOT a
# preamble string table.
# ---------------------------------------------------------------------------


_FIXTURE_CAPS: tuple[Capability, ...] = (
    Capability(
        id="SG_SEARCH",
        name="sg_search",
        description=(
            "Custom fixture capability exposed for the capability-surface test."
        ),
        input_schema=MappingProxyType({}),
    ),
)


class TestVariantBodyFromCapabilities:
    """The rendered variant resolves its tool surface against capabilities.py."""

    def test_default_render_includes_registered_capabilities(self) -> None:
        """Sanity: default rendering lists the real capability registry names."""
        body = _render_mcp_section()
        for expected in ("keyword_search", "symbol_references", "file_read"):
            assert expected in body, f"missing capability name {expected!r}"

    def test_fixture_capability_set_is_sole_source(self) -> None:
        """With a fixture capability set of {sg_search}, the rendered body
        must NOT contain any other concrete Sourcegraph tool names such as
        ``sg_find_references`` or ``sg_lookup_symbol`` — the preamble
        string table must not leak into the MCP variant.
        """
        from codeprobe.preambles import templates as preamble_templates

        with patch.object(
            preamble_templates, "list_capabilities", return_value=_FIXTURE_CAPS
        ):
            body = preamble_templates.render("mcp_base.md.j2")

        # The fixture capability must appear.
        assert "sg_search" in body

        # Forbidden: concrete Sourcegraph tool names from any old preamble
        # string table should NOT leak through the capability surface.
        for banned in ("sg_find_references", "sg_lookup_symbol", "nls_search"):
            assert banned not in body, (
                f"capability-rendered variant leaked {banned!r}; it must "
                f"only reference capabilities declared in capabilities.py"
            )


# ---------------------------------------------------------------------------
# Oracle-task path also gets the variant for org_scale tasks
# ---------------------------------------------------------------------------


class TestOracleOrgScaleVariant:
    """Oracle tasks with org_scale=True receive instruction_mcp.md too."""

    def test_oracle_org_scale_emits_variant(self, tmp_path: Path) -> None:
        task = _make_task(
            task_id="oracle001",
            task_type="org_scale_cross_repo",
            org_scale=True,
            issue_title="Find callers of foo",
            issue_body="List the files that call `foo`.",
            verification=TaskVerification(
                type="oracle",
                command="bash tests/test.sh",
                reward_type="continuous",
                oracle_type="file_list",
                oracle_answer=("pkg/a.py", "pkg/b.py"),
            ),
        )

        result_path = write_task_dir(task, tmp_path / "tasks", tmp_path / "myrepo")
        assert (result_path / "instruction.md").is_file()
        assert (result_path / "instruction_mcp.md").is_file()
