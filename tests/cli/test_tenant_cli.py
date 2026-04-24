"""CLI integration tests for ``--tenant`` flag + cache subgroup registration.

Covers the Phase E atomic rollout (tenant-cli-integration work unit):

- AC1: ``codeprobe cache --help`` exits 0 (group registered at the root).
- AC2-4: ``--tenant`` appears in the ``--help`` output for ``mine``,
  ``run``, and ``snapshot create``.
- AC5: bare ``codeprobe mine <repo> --json`` echoes a derived tenant
  into ``envelope.data.tenant`` with a known ``tenant_source``.
- AC6: ``CI=true`` without ``--tenant``/``CODEPROBE_TENANT`` surfaces the
  ``TENANT_REQUIRED_IN_CI`` terminal DiagnosticError in the envelope.
- AC7: ``CI=true`` + ``--tenant my-ci`` succeeds with source ``flag``.
- AC8: Inside a linked git worktree, source is
  ``git-remote+user+worktree`` and the tenant id ends in ``@<8-hex>``.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def isolated_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Isolate HOME + unset ambient CI/CODEPROBE_TENANT env vars.

    The tenant module reads ``os.environ`` indirectly via
    :func:`resolve_tenant`, so we strip anything that could leak into the
    derivation before each test.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("CODEPROBE_TENANT", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("USER", "alice")


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True)


def _git_init_with_commit(cwd: Path) -> None:
    _run(["git", "init", "-q", "-b", "main"], cwd)
    _run(["git", "config", "user.email", "test@example.com"], cwd)
    _run(["git", "config", "user.name", "Test User"], cwd)
    _run(["git", "config", "commit.gpgsign", "false"], cwd)
    _run(
        ["git", "remote", "add", "origin", "git@github.com:org/repo.git"],
        cwd,
    )
    (cwd / "README.md").write_text("seed\n")
    _run(["git", "add", "README.md"], cwd)
    _run(["git", "commit", "-q", "-m", "seed"], cwd)


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with an origin remote and one commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init_with_commit(repo)
    return repo


def _parse_envelope(output: str) -> Mapping[str, object]:
    """Extract the last JSON envelope from ``CliRunner.output``.

    Pretty output can bleed in during mine execution. The envelope is
    the last complete JSON object emitted on stdout when ``--json`` is
    passed, so we scan from the tail.
    """
    lines = [line for line in output.splitlines() if line.strip()]
    # Try to find a line that is itself a JSON object.
    for line in reversed(lines):
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict) and parsed.get("record_type") == "envelope":
                return parsed
        except json.JSONDecodeError:
            continue

    # Fall back: try to parse the whole stdout (multi-line pretty JSON).
    try:
        parsed = json.loads(output)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Last resort: find the first '{' and try to parse the tail as JSON.
    idx = output.rfind("{\n")
    if idx == -1:
        idx = output.find("{")
    if idx != -1:
        try:
            return json.loads(output[idx:])
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"No JSON envelope found in CLI output. Output was:\n{output}"
            ) from exc

    raise AssertionError(
        f"No JSON envelope found in CLI output. Output was:\n{output}"
    )


# ---------------------------------------------------------------------------
# AC1-4 — help surface wiring
# ---------------------------------------------------------------------------


def test_cache_group_registered(runner: CliRunner) -> None:
    """AC1: ``codeprobe cache --help`` exits 0 now that the group is attached."""
    result = runner.invoke(main, ["cache", "--help"])
    assert result.exit_code == 0, result.output
    assert "cache" in result.output.lower()
    assert "purge" in result.output.lower()


def test_mine_has_tenant_flag(runner: CliRunner) -> None:
    """AC2: ``--tenant`` listed in ``codeprobe mine --help``."""
    result = runner.invoke(main, ["mine", "--help"])
    assert result.exit_code == 0, result.output
    assert "--tenant" in result.output


def test_run_has_tenant_flag(runner: CliRunner) -> None:
    """AC3: ``--tenant`` listed in ``codeprobe run --help``."""
    result = runner.invoke(main, ["run", "--help"])
    assert result.exit_code == 0, result.output
    assert "--tenant" in result.output


def test_snapshot_create_has_tenant_flag(runner: CliRunner) -> None:
    """AC4: ``--tenant`` listed in ``codeprobe snapshot create --help``."""
    result = runner.invoke(main, ["snapshot", "create", "--help"])
    assert result.exit_code == 0, result.output
    assert "--tenant" in result.output


# ---------------------------------------------------------------------------
# AC5 — bare invocation derives tenant from git remote + user
# ---------------------------------------------------------------------------


_VALID_SOURCES = frozenset(
    {
        "env",
        "flag",
        "url-override+user",
        "git-remote+user",
        "git-remote+user+worktree",
        "cwd-hash+user",
    }
)


def test_bare_mine_echoes_derived_tenant(
    runner: CliRunner, tmp_git_repo: Path, isolated_env: None
) -> None:
    """AC5: Mine without ``--tenant`` fills in tenant/source in envelope."""
    result = runner.invoke(
        main,
        [
            "mine",
            str(tmp_git_repo),
            "--json",
            "--count",
            "3",
            "--no-llm",
            "--no-interactive",
        ],
    )
    # Mining a trivially tiny repo may exit with any of 0 (no tasks),
    # 2 (soft failure), or 1 — the important assertion is that the
    # envelope is well-formed and carries the tenant.
    envelope = _parse_envelope(result.output)
    data = envelope.get("data") or {}
    assert "tenant" in data, (
        f"expected 'tenant' key in envelope.data; got keys={list(data.keys())} "
        f"output={result.output!r}"
    )
    assert data["tenant"], f"tenant must be non-empty; got {data['tenant']!r}"
    assert data.get("tenant_source") in _VALID_SOURCES, (
        f"tenant_source={data.get('tenant_source')!r} not in {_VALID_SOURCES}"
    )


# ---------------------------------------------------------------------------
# AC6 — CI guard surfaces TENANT_REQUIRED_IN_CI terminal error
# ---------------------------------------------------------------------------


def test_ci_without_tenant_raises_terminal(
    runner: CliRunner, tmp_git_repo: Path, isolated_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC6: ``CI=true`` + no ``--tenant`` + no ``CODEPROBE_TENANT`` fails terminally."""
    monkeypatch.setenv("CI", "true")
    result = runner.invoke(
        main,
        [
            "mine",
            str(tmp_git_repo),
            "--json",
            "--count",
            "3",
            "--no-llm",
            "--no-interactive",
        ],
    )
    # The DiagnosticError must flow through the top-level handler into
    # the envelope. Exit code should be non-zero.
    assert result.exit_code != 0, result.output
    envelope = _parse_envelope(result.output)
    assert envelope.get("ok") is False, envelope
    err = envelope.get("error") or {}
    assert err.get("code") == "TENANT_REQUIRED_IN_CI", err
    assert err.get("terminal") is True, err


# ---------------------------------------------------------------------------
# AC7 — explicit --tenant wins over CI guard
# ---------------------------------------------------------------------------


def test_explicit_tenant_flag_wins(
    runner: CliRunner, tmp_git_repo: Path, isolated_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC7: ``CI=true`` + ``--tenant my-ci`` succeeds with source='flag'."""
    monkeypatch.setenv("CI", "true")
    result = runner.invoke(
        main,
        [
            "mine",
            str(tmp_git_repo),
            "--json",
            "--count",
            "3",
            "--no-llm",
            "--no-interactive",
            "--tenant",
            "my-ci",
        ],
    )
    envelope = _parse_envelope(result.output)
    data = envelope.get("data") or {}
    assert data.get("tenant") == "my-ci", (
        f"expected tenant=='my-ci'; got {data.get('tenant')!r} in {envelope}"
    )
    assert data.get("tenant_source") == "flag", (
        f"expected tenant_source=='flag'; got {data.get('tenant_source')!r}"
    )


# ---------------------------------------------------------------------------
# AC8 — worktree suffix derivation
# ---------------------------------------------------------------------------


def test_worktree_suffix_in_tenant(
    runner: CliRunner, tmp_path: Path, isolated_env: None
) -> None:
    """AC8: Inside a git worktree, source='git-remote+user+worktree'."""
    main_repo = tmp_path / "main_repo"
    main_repo.mkdir()
    _git_init_with_commit(main_repo)

    wt = tmp_path / "worktree"
    _run(
        ["git", "worktree", "add", "-b", "feature-branch", str(wt)],
        main_repo,
    )

    result = runner.invoke(
        main,
        [
            "mine",
            str(wt),
            "--json",
            "--count",
            "3",
            "--no-llm",
            "--no-interactive",
        ],
    )
    envelope = _parse_envelope(result.output)
    data = envelope.get("data") or {}
    assert data.get("tenant_source") == "git-remote+user+worktree", (
        f"expected source='git-remote+user+worktree'; got "
        f"{data.get('tenant_source')!r} in {envelope}"
    )
    tenant = data.get("tenant") or ""
    parts = str(tenant).split("@")
    assert len(parts) == 3, (
        f"worktree tenant should have 3 '@'-separated segments "
        f"(slug@user@hash); got {tenant!r}"
    )
    suffix = parts[-1]
    assert len(suffix) == 8, (
        f"worktree suffix must be 8 chars; got {suffix!r} (len={len(suffix)})"
    )
    # Suffix must parse as hex.
    int(suffix, 16)
