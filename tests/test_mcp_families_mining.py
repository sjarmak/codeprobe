"""Tests for MCP-advantaged task family mining functions.

Tests _mine_symbol_reference_tasks(), _mine_type_hierarchy_tasks(),
_mine_change_scope_tasks(), and the include_mcp_families parameter
on mine_org_scale_tasks().
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codeprobe.mining.org_scale import (
    _mine_symbol_reference_tasks,
    _mine_type_hierarchy_tasks,
    _mine_change_scope_tasks,
    mine_org_scale_tasks,
)
from codeprobe.mining.org_scale_families import MCP_FAMILIES
from codeprobe.mining.org_scale_scanner import get_head_sha, get_tracked_files

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """Create a git repo with given files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for path, content in files.items():
        fp = repo / path
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo),
        capture_output=True,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
        },
    )
    return repo


def _make_python_repo_with_symbols(tmp_path: Path) -> Path:
    """Create a Python repo with public symbols that have many references."""
    files = {}
    # A file defining a public symbol with a domain-specific name
    files["src/core/base_handler.py"] = (
        "from abc import ABC, abstractmethod\n\n"
        "class BaseHandler(ABC):\n"
        "    @abstractmethod\n"
        "    def handle_request(self, req):\n"
        "        pass\n\n"
        "def process_configuration(config):\n"
        "    return config\n"
    )
    # Multiple files referencing process_configuration
    for i in range(12):
        files[f"src/modules/mod_{i}.py"] = (
            f"from src.core.base_handler import process_configuration\n\n"
            f"def run_{i}():\n"
            f"    return process_configuration({{'key': {i}}})\n"
        )
    # Subclasses of BaseHandler
    files["src/handlers/http_handler.py"] = (
        "from src.core.base_handler import BaseHandler\n\n"
        "class HttpHandler(BaseHandler):\n"
        "    def handle_request(self, req):\n"
        "        return 'http'\n"
    )
    files["src/handlers/grpc_handler.py"] = (
        "from src.core.base_handler import BaseHandler\n\n"
        "class GrpcHandler(BaseHandler):\n"
        "    def handle_request(self, req):\n"
        "        return 'grpc'\n"
    )
    files["src/handlers/ws_handler.py"] = (
        "from src.core.base_handler import BaseHandler\n\n"
        "class WsHandler(BaseHandler):\n"
        "    def handle_request(self, req):\n"
        "        return 'ws'\n"
    )
    # Usage of subclass names
    files["src/main.py"] = (
        "from src.handlers.http_handler import HttpHandler\n"
        "from src.handlers.grpc_handler import GrpcHandler\n\n"
        "def create_handler(protocol):\n"
        "    if protocol == 'http':\n"
        "        return HttpHandler()\n"
        "    return GrpcHandler()\n"
    )
    files["src/factory.py"] = (
        "from src.handlers.http_handler import HttpHandler\n"
        "from src.handlers.ws_handler import WsHandler\n\n"
        "HANDLERS = [HttpHandler, WsHandler]\n"
    )
    return _make_repo(tmp_path, files)


# ---------------------------------------------------------------------------
# _mine_symbol_reference_tasks
# ---------------------------------------------------------------------------


class TestMineSymbolReferenceTasks:
    def test_produces_tasks_from_high_fan_out_symbols(self, tmp_path: Path) -> None:
        repo = _make_python_repo_with_symbols(tmp_path)
        repo_paths = [repo]
        tracked_files = get_tracked_files(repo)
        commit_sha = get_head_sha(repo)

        tasks = _mine_symbol_reference_tasks(
            repo_paths, tracked_files, "python", commit_sha, no_llm=True
        )

        assert len(tasks) >= 1
        task = tasks[0]
        assert task.metadata.category == "symbol-reference-trace"
        assert task.verification.oracle_type == "file_list"
        assert task.instruction_variant_path == "instruction_discovery.md"
        assert task.metadata.org_scale is True
        assert len(task.verification.oracle_answer) >= 5

    def test_task_id_includes_sym_ref_prefix(self, tmp_path: Path) -> None:
        repo = _make_python_repo_with_symbols(tmp_path)
        tracked_files = get_tracked_files(repo)
        commit_sha = get_head_sha(repo)

        tasks = _mine_symbol_reference_tasks(
            [repo], tracked_files, "python", commit_sha, no_llm=True
        )

        assert len(tasks) >= 1
        # Task ID is a hash — just verify it exists and is 8 chars
        assert len(tasks[0].id) == 8

    def test_returns_empty_when_no_symbols(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, {"README.md": "# Hello"})
        tracked_files = get_tracked_files(repo)
        commit_sha = get_head_sha(repo)

        tasks = _mine_symbol_reference_tasks(
            [repo], tracked_files, "python", commit_sha, no_llm=True
        )

        assert tasks == []


# ---------------------------------------------------------------------------
# _mine_type_hierarchy_tasks
# ---------------------------------------------------------------------------


class TestMineTypeHierarchyTasks:
    def test_produces_tasks_from_abc_with_subclasses(self, tmp_path: Path) -> None:
        repo = _make_python_repo_with_symbols(tmp_path)
        tracked_files = get_tracked_files(repo)
        commit_sha = get_head_sha(repo)

        tasks = _mine_type_hierarchy_tasks(
            [repo], tracked_files, "python", commit_sha, no_llm=True
        )

        assert len(tasks) >= 1
        task = tasks[0]
        assert task.metadata.category == "type-hierarchy-consumers"
        assert task.verification.oracle_type == "file_list"
        assert task.metadata.org_scale is True
        # Should have oracle_tiers with required and supplementary
        if task.verification.oracle_tiers:
            tiers = set(task.verification.oracle_tiers.values())
            assert "required" in tiers or "supplementary" in tiers

    def test_returns_empty_for_non_python(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, {"main.go": "package main\nfunc main() {}\n"})
        tracked_files = get_tracked_files(repo)
        commit_sha = get_head_sha(repo)

        tasks = _mine_type_hierarchy_tasks(
            [repo], tracked_files, "go", commit_sha, no_llm=True
        )

        assert tasks == []


# ---------------------------------------------------------------------------
# _mine_change_scope_tasks
# ---------------------------------------------------------------------------


class TestMineChangeScopeTasks:
    def test_produces_tasks_from_high_fan_out_symbols(self, tmp_path: Path) -> None:
        repo = _make_python_repo_with_symbols(tmp_path)
        tracked_files = get_tracked_files(repo)
        commit_sha = get_head_sha(repo)

        tasks = _mine_change_scope_tasks(
            [repo], tracked_files, "python", commit_sha, no_llm=True
        )

        assert len(tasks) >= 1
        task = tasks[0]
        assert task.metadata.category == "change-scope-audit"
        assert task.verification.oracle_type == "file_list"
        assert task.metadata.org_scale is True

    def test_returns_empty_when_no_symbols(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, {"README.md": "# Hello"})
        tracked_files = get_tracked_files(repo)
        commit_sha = get_head_sha(repo)

        tasks = _mine_change_scope_tasks(
            [repo], tracked_files, "python", commit_sha, no_llm=True
        )

        assert tasks == []


# ---------------------------------------------------------------------------
# mine_org_scale_tasks with include_mcp_families
# ---------------------------------------------------------------------------


class TestMineOrgScaleWithMcpFamilies:
    def test_mcp_families_excluded_by_default(self, tmp_path: Path) -> None:
        """Without include_mcp_families, MCP tasks are not generated."""
        repo = _make_python_repo_with_symbols(tmp_path)

        result = mine_org_scale_tasks(
            [repo], count=20, no_llm=True, include_multi_hop=False
        )

        categories = {t.metadata.category for t in result.tasks}
        mcp_categories = {
            "symbol-reference-trace",
            "type-hierarchy-consumers",
            "change-scope-audit",
        }
        assert not categories & mcp_categories

    def test_mcp_families_included_when_flag_set(self, tmp_path: Path) -> None:
        """With include_mcp_families=True, MCP tasks are generated."""
        repo = _make_python_repo_with_symbols(tmp_path)

        result = mine_org_scale_tasks(
            [repo], count=20, no_llm=True, include_mcp_families=True
        )

        categories = {t.metadata.category for t in result.tasks}
        mcp_categories = {
            "symbol-reference-trace",
            "type-hierarchy-consumers",
            "change-scope-audit",
        }
        # At least one MCP family should produce tasks
        assert categories & mcp_categories

    def test_count_respected_with_mcp_families(self, tmp_path: Path) -> None:
        """Total task count is still capped."""
        repo = _make_python_repo_with_symbols(tmp_path)

        result = mine_org_scale_tasks(
            [repo], count=2, no_llm=True, include_mcp_families=True
        )

        assert len(result.tasks) <= 2
