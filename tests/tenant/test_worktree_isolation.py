"""Worktree isolation tests for :func:`codeprobe.tenant.derive_tenant`.

Verifies AC9: a linked worktree derives a tenant id DISTINCT from the
one computed inside the main repo checkout, with source variant
``git-remote+user+worktree`` and an 8-hex-char suffix.
"""

from __future__ import annotations

import subprocess

import pytest

from codeprobe.paths import _validate_tenant_id
from codeprobe.tenant import derive_tenant


def _run(cmd: list[str], cwd) -> None:
    subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True)


def _git_init_with_commit(cwd) -> None:
    _run(["git", "init", "-q", "-b", "main"], cwd)
    _run(["git", "config", "user.email", "test@example.com"], cwd)
    _run(["git", "config", "user.name", "Test User"], cwd)
    _run(["git", "config", "commit.gpgsign", "false"], cwd)
    _run(
        ["git", "remote", "add", "origin", "git@github.com:org/repo.git"],
        cwd,
    )
    # Need a commit before ``git worktree add`` can point at a branch.
    (cwd / "README.md").write_text("seed\n")
    _run(["git", "add", "README.md"], cwd)
    _run(["git", "commit", "-q", "-m", "seed"], cwd)


@pytest.fixture
def repo_with_worktree(tmp_path):
    """Create a main repo + a linked worktree sibling directory.

    Returns ``(main_path, worktree_path)``.
    """

    main = tmp_path / "main"
    main.mkdir()
    _git_init_with_commit(main)

    wt = tmp_path / "wt"
    _run(
        ["git", "worktree", "add", "-b", "feature-branch", str(wt)],
        main,
    )
    return main, wt


def test_worktree_source_variant(repo_with_worktree):
    """Source is 'git-remote+user+worktree' inside a linked worktree."""

    _main, wt = repo_with_worktree
    env = {"USER": "alice"}

    tenant, source = derive_tenant(wt, env)

    assert source == "git-remote+user+worktree"
    _validate_tenant_id(tenant)


def test_worktree_id_ends_with_8hex(repo_with_worktree):
    """Worktree tenant id ends with ``@<8-char-hex>`` suffix."""

    _main, wt = repo_with_worktree
    env = {"USER": "alice"}

    tenant, _source = derive_tenant(wt, env)

    # Last @-separated component is the worktree hash
    parts = tenant.split("@")
    assert len(parts) == 3, f"expected slug@user@hash, got {tenant!r}"
    slug, user, suffix = parts
    assert user == "alice"
    assert len(suffix) == 8
    int(suffix, 16)  # raises ValueError when not hex
    # Slug is the normal remote slug, untouched by worktree derivation.
    assert slug == "github.com-org-repo"


def test_worktree_distinct_from_main(repo_with_worktree):
    """AC9: worktree tenant differs from the main-repo tenant."""

    main, wt = repo_with_worktree
    env = {"USER": "alice"}

    main_tenant, main_src = derive_tenant(main, env)
    wt_tenant, wt_src = derive_tenant(wt, env)

    assert main_src == "git-remote+user"
    assert wt_src == "git-remote+user+worktree"
    assert main_tenant != wt_tenant
    # Main tenant has no worktree suffix — exactly one '@'.
    assert main_tenant.count("@") == 1
    # Worktree tenant has two '@' separators.
    assert wt_tenant.count("@") == 2


def test_two_worktrees_distinct(tmp_path):
    """Sibling worktrees of the same repo produce distinct tenant ids."""

    main = tmp_path / "main"
    main.mkdir()
    _git_init_with_commit(main)

    wt_a = tmp_path / "wt-a"
    wt_b = tmp_path / "wt-b"
    _run(["git", "worktree", "add", "-b", "branch-a", str(wt_a)], main)
    _run(["git", "worktree", "add", "-b", "branch-b", str(wt_b)], main)

    env = {"USER": "alice"}
    a_tenant, _ = derive_tenant(wt_a, env)
    b_tenant, _ = derive_tenant(wt_b, env)

    assert a_tenant != b_tenant
    # Both share the same slug and user prefix.
    assert a_tenant.rsplit("@", 1)[0] == b_tenant.rsplit("@", 1)[0]
