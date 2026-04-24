"""Tests for ``codeprobe.paths`` — tenant-scoped filesystem invariants (INV2)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from codeprobe.paths import (
    CrossTenantAccessError,
    assert_tenant_owned,
    tenant_root,
    tenant_state_dir,
)

_HASH_RE = re.compile(r"^[0-9a-f]{16}$")


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``HOME`` at a temp directory so ~/.codeprobe resolves there."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


class TestTenantStateDir:
    """Exercise :func:`tenant_state_dir` shape and determinism."""

    def test_path_shape(self, tmp_home: Path) -> None:
        path = tenant_state_dir(
            "acme", "https://github.com/x/y", "main", "/tmp/wt"
        )
        # ~/.codeprobe/state/acme/<hash>
        assert path.parent.parent == tmp_home / ".codeprobe" / "state"
        assert path.parent.name == "acme"
        assert _HASH_RE.match(path.name), (
            f"expected 16-char hex, got {path.name!r}"
        )

    def test_determinism(self, tmp_home: Path) -> None:
        a = tenant_state_dir("acme", "https://github.com/x/y", "main", "/tmp/wt")
        b = tenant_state_dir("acme", "https://github.com/x/y", "main", "/tmp/wt")
        assert a == b

    def test_different_inputs_differ(self, tmp_home: Path) -> None:
        a = tenant_state_dir("acme", "https://github.com/x/y", "main", "/tmp/wt")
        b = tenant_state_dir("acme", "https://github.com/x/y", "dev", "/tmp/wt")
        c = tenant_state_dir("acme", "https://github.com/x/z", "main", "/tmp/wt")
        d = tenant_state_dir("acme", "https://github.com/x/y", "main", "/tmp/other")
        assert len({a, b, c, d}) == 4

    def test_different_tenants_have_different_parents(self, tmp_home: Path) -> None:
        a = tenant_state_dir("acme", "https://github.com/x/y", "main", "/tmp/wt")
        b = tenant_state_dir("globex", "https://github.com/x/y", "main", "/tmp/wt")
        assert a.parent != b.parent
        # Hash is the same (same repo/ref/worktree); only tenant segment differs.
        assert a.name == b.name


class TestTenantRoot:
    def test_under_home_codeprobe_state(self, tmp_home: Path) -> None:
        assert tenant_root("acme") == tmp_home / ".codeprobe" / "state" / "acme"

    def test_rejects_path_separator(self, tmp_home: Path) -> None:
        with pytest.raises(ValueError):
            tenant_root("../escape")

    def test_rejects_empty(self, tmp_home: Path) -> None:
        with pytest.raises(ValueError):
            tenant_root("")

    def test_rejects_whitespace(self, tmp_home: Path) -> None:
        with pytest.raises(ValueError):
            tenant_root("   ")

    def test_rejects_dotdot(self, tmp_home: Path) -> None:
        with pytest.raises(ValueError):
            tenant_root("..")


class TestAssertTenantOwned:
    """Fail-closed cross-tenant boundary enforcement."""

    def test_accepts_path_inside_tenant(self, tmp_home: Path) -> None:
        path = tenant_state_dir(
            "acme", "https://github.com/x/y", "main", "/tmp/wt"
        )
        # Should not raise.
        assert_tenant_owned(path, "acme")

    def test_accepts_nested_file(self, tmp_home: Path) -> None:
        root = tenant_root("acme")
        nested = root / "some" / "nested" / "artifact.json"
        assert_tenant_owned(nested, "acme")

    def test_rejects_other_tenant(self, tmp_home: Path) -> None:
        other_path = tenant_state_dir(
            "globex", "https://github.com/x/y", "main", "/tmp/wt"
        )
        with pytest.raises(CrossTenantAccessError):
            assert_tenant_owned(other_path, "acme")

    def test_rejects_path_outside_codeprobe_root(self, tmp_home: Path) -> None:
        with pytest.raises(CrossTenantAccessError):
            assert_tenant_owned(Path("/etc/passwd"), "acme")

    def test_rejects_parent_traversal(self, tmp_home: Path) -> None:
        # A path with .. that, when resolved, escapes the tenant root.
        sneaky = tenant_root("acme") / ".." / "globex" / "leak.json"
        with pytest.raises(CrossTenantAccessError):
            assert_tenant_owned(sneaky, "acme")
