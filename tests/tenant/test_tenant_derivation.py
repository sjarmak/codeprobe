"""Tests for :func:`codeprobe.tenant.derive_tenant`.

Covers every non-worktree acceptance criterion from the tenant-module
work unit: env precedence, explicit flag, URL override, git-remote
derivation (SSH + HTTPS), cwd-hash fallback, CI guard, and USER fallback
chain.
"""

from __future__ import annotations

import subprocess

import pytest

from codeprobe.paths import _validate_tenant_id
from codeprobe.tenant import DiagnosticError, derive_tenant


def _run(cmd: list[str], cwd) -> None:
    """Run a git command in ``cwd``, raising on non-zero exit."""

    subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True)


def _git_init(cwd) -> None:
    """Initialize a git repo with deterministic config."""

    _run(["git", "init", "-q", "-b", "main"], cwd)
    _run(["git", "config", "user.email", "test@example.com"], cwd)
    _run(["git", "config", "user.name", "Test User"], cwd)
    _run(["git", "config", "commit.gpgsign", "false"], cwd)


def test_env_wins_over_everything(tmp_path):
    """AC2: CODEPROBE_TENANT env var trumps any derivation path."""

    _git_init(tmp_path)
    _run(
        ["git", "remote", "add", "origin", "git@github.com:org/repo.git"],
        tmp_path,
    )
    env = {"CODEPROBE_TENANT": "custom-id", "USER": "alice"}

    tenant, source = derive_tenant(tmp_path, env)

    assert tenant == "custom-id"
    assert source == "env"


def test_explicit_flag_wins_when_no_env(tmp_path):
    """AC3: ``explicit_flag`` beats derivation but loses to env."""

    env = {"USER": "alice"}
    tenant, source = derive_tenant(tmp_path, env, explicit_flag="manual")

    assert tenant == "manual"
    assert source == "flag"


def test_env_still_wins_over_explicit_flag(tmp_path):
    """Priority rule: env > flag."""

    env = {"CODEPROBE_TENANT": "envval", "USER": "alice"}
    tenant, source = derive_tenant(tmp_path, env, explicit_flag="flagval")

    assert tenant == "envval"
    assert source == "env"


def test_url_override_non_git(tmp_path):
    """AC4: url_override in a non-git tempdir produces slug+user."""

    env = {"USER": "alice"}
    tenant, source = derive_tenant(
        tmp_path,
        env,
        url_override="https://github.com/org/repo.git",
    )

    assert source == "url-override+user"
    assert tenant.endswith("@alice")
    assert "github.com" in tenant
    assert "org" in tenant
    assert "repo" in tenant


def test_git_remote_ssh(tmp_path):
    """AC5: SSH-form origin produces ``github.com-org-repo@alice``."""

    _git_init(tmp_path)
    _run(
        ["git", "remote", "add", "origin", "git@github.com:org/repo.git"],
        tmp_path,
    )
    env = {"USER": "alice"}

    tenant, source = derive_tenant(tmp_path, env)

    assert source == "git-remote+user"
    assert tenant == "github.com-org-repo@alice"


def test_git_remote_https(tmp_path):
    """HTTPS form should normalize to the same slug as SSH form."""

    _git_init(tmp_path)
    _run(
        ["git", "remote", "add", "origin", "https://github.com/org/repo.git"],
        tmp_path,
    )
    env = {"USER": "alice"}

    tenant, source = derive_tenant(tmp_path, env)

    assert source == "git-remote+user"
    assert tenant == "github.com-org-repo@alice"


def test_git_remote_https_no_dotgit(tmp_path):
    """HTTPS URL without .git suffix should still normalize correctly."""

    _git_init(tmp_path)
    _run(
        ["git", "remote", "add", "origin", "https://github.com/org/repo"],
        tmp_path,
    )
    env = {"USER": "alice"}

    tenant, _source = derive_tenant(tmp_path, env)

    assert tenant == "github.com-org-repo@alice"


def test_git_remote_strips_userinfo(tmp_path):
    """Credentials embedded in HTTPS URLs must not leak into the tenant id."""

    _git_init(tmp_path)
    _run(
        [
            "git",
            "remote",
            "add",
            "origin",
            "https://secretuser:secrettoken@github.com/org/repo.git",
        ],
        tmp_path,
    )
    env = {"USER": "alice"}

    tenant, _source = derive_tenant(tmp_path, env)

    assert "secretuser" not in tenant
    assert "secrettoken" not in tenant
    assert tenant == "github.com-org-repo@alice"


def test_cwd_hash_fallback(tmp_path):
    """AC6: non-git cwd with no url_override yields ``cwd-<12hex>@alice``."""

    env = {"USER": "alice"}
    tenant, source = derive_tenant(tmp_path, env)

    assert source == "cwd-hash+user"
    assert tenant.endswith("@alice")
    assert tenant.startswith("cwd-")
    prefix, _, suffix = tenant.partition("@")
    assert suffix == "alice"
    # 'cwd-' + 12 hex chars
    assert len(prefix) == 4 + 12
    hex_part = prefix[4:]
    int(hex_part, 16)  # raises ValueError if not hex


def test_ci_guard_raises(tmp_path):
    """AC7: CI=true with no tenant + no flag raises DiagnosticError."""

    env = {"CI": "true", "USER": "alice"}
    with pytest.raises(DiagnosticError) as excinfo:
        derive_tenant(tmp_path, env)

    err = excinfo.value
    assert getattr(err, "code", None) == "TENANT_REQUIRED_IN_CI"
    assert getattr(err, "terminal", False) is True


def test_ci_guard_github_actions(tmp_path):
    """GITHUB_ACTIONS=true also triggers the guard."""

    env = {"GITHUB_ACTIONS": "true", "USER": "alice"}
    with pytest.raises(DiagnosticError):
        derive_tenant(tmp_path, env)


def test_ci_with_explicit_tenant(tmp_path):
    """AC8: CI with CODEPROBE_TENANT set returns env value, no raise."""

    env = {"CI": "true", "CODEPROBE_TENANT": "ci-abc"}
    tenant, source = derive_tenant(tmp_path, env)

    assert tenant == "ci-abc"
    assert source == "env"


def test_ci_with_explicit_flag(tmp_path):
    """CI with --tenant flag also bypasses the guard."""

    env = {"CI": "true", "USER": "alice"}
    tenant, source = derive_tenant(tmp_path, env, explicit_flag="ci-flag")

    assert tenant == "ci-flag"
    assert source == "flag"


def test_user_fallback_username(tmp_path):
    """AC11: USERNAME is used when USER is absent."""

    env = {"USERNAME": "bob"}
    tenant, _source = derive_tenant(tmp_path, env)

    assert tenant.endswith("@bob")


def test_user_fallback_unknown(tmp_path):
    """AC12: neither USER nor USERNAME present → @unknown."""

    env: dict[str, str] = {}
    tenant, _source = derive_tenant(tmp_path, env)

    assert tenant.endswith("@unknown")


def test_user_sanitized(tmp_path):
    """Non-filesystem-safe chars in USER are replaced, not propagated."""

    env = {"USER": "al/ice"}
    tenant, _source = derive_tenant(tmp_path, env)

    # '/' becomes '-'; validator would reject a slash-bearing id.
    _validate_tenant_id(tenant)
    assert "/" not in tenant


def test_all_ids_pass_validator(tmp_path, monkeypatch):
    """AC10: every source variant returns a validator-approved id."""

    # env variant
    t, _ = derive_tenant(tmp_path, {"CODEPROBE_TENANT": "envid"})
    _validate_tenant_id(t)

    # flag variant
    t, _ = derive_tenant(tmp_path, {"USER": "a"}, explicit_flag="flagid")
    _validate_tenant_id(t)

    # url-override variant
    t, _ = derive_tenant(
        tmp_path,
        {"USER": "alice"},
        url_override="https://github.com/a/b.git",
    )
    _validate_tenant_id(t)

    # cwd-hash variant
    t, _ = derive_tenant(tmp_path, {"USER": "alice"})
    _validate_tenant_id(t)

    # git-remote variant
    _git_init(tmp_path)
    _run(
        ["git", "remote", "add", "origin", "git@github.com:org/repo.git"],
        tmp_path,
    )
    t, src = derive_tenant(tmp_path, {"USER": "alice"})
    assert src == "git-remote+user"
    _validate_tenant_id(t)


def test_long_url_is_capped(tmp_path):
    """Deeply nested paths must not exceed the 64-char cap."""

    deep_path = "/".join(f"seg{i:03d}" for i in range(30))
    url = f"https://gitlab.example.com/{deep_path}.git"
    tenant, _source = derive_tenant(
        tmp_path, {"USER": "alice"}, url_override=url
    )

    from codeprobe.tenant import MAX_TENANT_LEN

    assert len(tenant) <= MAX_TENANT_LEN
    _validate_tenant_id(tenant)


def test_no_remote_falls_back_to_cwd_hash(tmp_path):
    """git repo without an ``origin`` remote falls through to cwd-hash."""

    _git_init(tmp_path)
    env = {"USER": "alice"}

    tenant, source = derive_tenant(tmp_path, env)

    assert source == "cwd-hash+user"
    assert tenant.startswith("cwd-")
