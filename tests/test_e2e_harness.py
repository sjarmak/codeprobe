"""Unit tests for the pure helpers behind the self-serve E2E harness.

The harness itself (``scripts/e2e/self_serve_acceptance.py``) IS the
integration test — these tests exercise only its deterministic building
blocks in isolation, so a regression in fixture generation, the network
guard, or envelope parsing is caught in milliseconds instead of the
harness's ~20s full run.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
from pathlib import Path
from types import ModuleType

import pytest

from codeprobe.mining._lang import detect_repo_language

_E2E_DIR = Path(__file__).resolve().parent.parent / "scripts" / "e2e"


def _load_module(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _E2E_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mfr = _load_module("e2e_make_fixture_repo", "make_fixture_repo.py")


# ---------------------------------------------------------------------------
# make_fixture_repo: git log count and language census
# ---------------------------------------------------------------------------


def _commit_count(repo: Path) -> int:
    import subprocess

    result = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip())


def test_python_fixture_commit_count_matches_feature_count(tmp_path: Path) -> None:
    repo = mfr.make_python_fixture_repo(tmp_path / "repo", feature_count=3)
    # 1 init commit + (1 feature commit + 1 merge commit) per feature.
    assert _commit_count(repo) == 1 + 2 * 3


def test_python_fixture_default_feature_count_clears_min_paired_tasks(
    tmp_path: Path,
) -> None:
    """DEFAULT_FEATURE_COUNT must stay >= 3 (stats.py's _MIN_PAIRED_TASKS)
    plus margin for mining yield not being 1:1 with merge count — a
    regression here would silently turn the harness's positive journey
    into a REFUSED comparison instead of a real one."""
    assert mfr.DEFAULT_FEATURE_COUNT >= 3


def test_python_fixture_language_is_python(tmp_path: Path) -> None:
    repo = mfr.make_python_fixture_repo(tmp_path / "repo", feature_count=2)
    assert detect_repo_language(repo) == "python"


def test_ruby_fixture_language_is_not_in_supported_matrix(tmp_path: Path) -> None:
    from codeprobe.mining._lang import SUPPORTED_MINING_LANGUAGES

    repo = mfr.make_ruby_fixture_repo(tmp_path / "repo")
    assert detect_repo_language(repo) not in SUPPORTED_MINING_LANGUAGES


def test_python_fixture_is_clean_by_default(tmp_path: Path) -> None:
    import subprocess

    repo = mfr.make_python_fixture_repo(tmp_path / "repo", feature_count=2)
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout.strip() == ""


def test_python_fixture_dirty_flag_leaves_untracked_file(tmp_path: Path) -> None:
    import subprocess

    repo = mfr.make_python_fixture_repo(tmp_path / "repo", feature_count=2, dirty=True)
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout.strip() != ""


def test_python_fixture_merge_commits_have_no_ff_shape(tmp_path: Path) -> None:
    """Each feature merge must be a real --no-ff merge commit (2 parents) —
    a fast-forward would collapse the PR-shaped history the miner expects."""
    import subprocess

    repo = mfr.make_python_fixture_repo(tmp_path / "repo", feature_count=2)
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "--merges", "--format=%P"],
        capture_output=True,
        text=True,
        check=True,
    )
    merge_lines = [ln for ln in log.stdout.splitlines() if ln.strip()]
    assert len(merge_lines) == 2
    assert all(len(ln.split()) == 2 for ln in merge_lines)


def test_fixture_commits_are_deterministic_across_two_runs(tmp_path: Path) -> None:
    """Two independent generations with the same parameters must produce
    identical commit trees (the harness's task-id/count assertions depend
    on this — a non-deterministic generator would make CI flaky)."""
    import subprocess

    def tree_hashes(repo: Path) -> list[str]:
        result = subprocess.run(
            ["git", "-C", str(repo), "log", "--format=%T"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.splitlines()

    repo_a = mfr.make_python_fixture_repo(tmp_path / "a", feature_count=3)
    repo_b = mfr.make_python_fixture_repo(tmp_path / "b", feature_count=3)
    assert tree_hashes(repo_a) == tree_hashes(repo_b)


def test_make_python_fixture_repo_rejects_nonempty_dest_via_cli(tmp_path: Path) -> None:
    dest = tmp_path / "occupied"
    dest.mkdir()
    (dest / "stray.txt").write_text("pre-existing\n")
    exit_code = mfr.main([str(dest)])
    assert exit_code == 2


# ---------------------------------------------------------------------------
# sitecustomize guard: blocks non-loopback, permits loopback
# ---------------------------------------------------------------------------


@pytest.fixture
def loaded_guard(tmp_path: Path):
    """Load sitecustomize_template.py, then restore the original socket
    functions afterward — the module applies its patches unconditionally
    at import time (by design: it must behave identically to a real
    ``sitecustomize.py`` on interpreter startup), so this fixture is the
    only thing standing between one test's guard and every other test in
    this process's socket module."""
    original_create_connection = socket.create_connection
    original_getaddrinfo = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    netlog = tmp_path / "netlog.txt"
    netlog.write_text("")  # pre-created, same as the real harness does
    import os

    os.environ["CODEPROBE_E2E_NETLOG"] = str(netlog)
    try:
        module = _load_module("e2e_sitecustomize_under_test", "sitecustomize_template.py")
        yield module, netlog
    finally:
        socket.create_connection = original_create_connection
        socket.getaddrinfo = original_getaddrinfo
        socket.socket.connect = original_connect
        socket.socket.connect_ex = original_connect_ex
        del os.environ["CODEPROBE_E2E_NETLOG"]
        sys.modules.pop("e2e_sitecustomize_under_test", None)


def test_guard_blocks_non_loopback_getaddrinfo(loaded_guard) -> None:
    _module, netlog = loaded_guard
    with pytest.raises(OSError, match="sitecustomize guard blocked"):
        socket.getaddrinfo("example.invalid", 443)
    assert "getaddrinfo" in netlog.read_text()
    assert "example.invalid" in netlog.read_text()


def test_guard_blocks_non_loopback_create_connection(loaded_guard) -> None:
    _module, netlog = loaded_guard
    with pytest.raises(OSError, match="sitecustomize guard blocked"):
        socket.create_connection(("example.invalid", 443), timeout=1)
    assert "create_connection" in netlog.read_text()


def test_guard_permits_loopback_connect(loaded_guard) -> None:
    """A loopback connect attempt must reach the real socket machinery
    (and fail with a normal connection error, e.g. nothing listening —
    never the guard's blocked-network OSError, and never logged)."""
    _module, netlog = loaded_guard
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError) as exc_info:
            sock.settimeout(1)
            sock.connect(("127.0.0.1", 1))  # port 1: reliably nothing listening
        assert "sitecustomize guard blocked" not in str(exc_info.value)
    finally:
        sock.close()
    assert netlog.read_text() == ""


def test_guard_permits_loopback_getaddrinfo(loaded_guard) -> None:
    _module, netlog = loaded_guard
    socket.getaddrinfo("127.0.0.1", 80)  # must not raise
    assert netlog.read_text() == ""


def test_guard_connect_ex_returns_errno_without_raising(loaded_guard) -> None:
    """connect_ex must never raise (that's the whole point of the _ex
    variant) — a non-loopback attempt gets a plain ECONNREFUSED-style
    errno back, logged, but silent."""
    _module, netlog = loaded_guard
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        result = sock.connect_ex(("93.184.216.34", 443))  # example.com's IP, non-loopback
    finally:
        sock.close()
    assert result != 0
    assert "connect_ex" in netlog.read_text()


def test_guard_is_a_noop_when_netlog_env_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEPROBE_E2E_NETLOG", raising=False)
    original_getaddrinfo = socket.getaddrinfo
    try:
        _load_module("e2e_sitecustomize_no_log", "sitecustomize_template.py")
        with pytest.raises(OSError):
            socket.getaddrinfo("example.invalid", 443)  # still blocks; just doesn't log
    finally:
        socket.getaddrinfo = original_getaddrinfo
        sys.modules.pop("e2e_sitecustomize_no_log", None)


# ---------------------------------------------------------------------------
# Envelope-assertion helpers reject missing fields
# ---------------------------------------------------------------------------


ssa = _load_module("e2e_self_serve_acceptance", "self_serve_acceptance.py")


def test_assert_ok_accepts_a_true_envelope() -> None:
    data = ssa._assert_ok({"ok": True, "data": {"x": 1}}, "ctx")
    assert data == {"x": 1}


def test_assert_ok_rejects_missing_ok_field() -> None:
    with pytest.raises(ssa.HarnessAssertionError, match="ctx"):
        ssa._assert_ok({"data": {}}, "ctx")


def test_assert_ok_rejects_false_ok() -> None:
    with pytest.raises(ssa.HarnessAssertionError):
        ssa._assert_ok({"ok": False, "error": {"code": "X"}}, "ctx")


def test_assert_ok_defaults_missing_data_to_empty_dict() -> None:
    assert ssa._assert_ok({"ok": True}, "ctx") == {}


def test_assert_error_code_accepts_matching_code() -> None:
    error = ssa._assert_error_code(
        {"ok": False, "error": {"code": "DIRTY_CHECKOUT"}}, "DIRTY_CHECKOUT", "ctx"
    )
    assert error["code"] == "DIRTY_CHECKOUT"


def test_assert_error_code_rejects_ok_true() -> None:
    with pytest.raises(ssa.HarnessAssertionError, match="expected a refusal"):
        ssa._assert_error_code({"ok": True}, "DIRTY_CHECKOUT", "ctx")


def test_assert_error_code_rejects_wrong_code() -> None:
    with pytest.raises(ssa.HarnessAssertionError, match="DIRTY_CHECKOUT"):
        ssa._assert_error_code(
            {"ok": False, "error": {"code": "OTHER_CODE"}}, "DIRTY_CHECKOUT", "ctx"
        )


def test_assert_error_code_rejects_missing_error_field() -> None:
    with pytest.raises(ssa.HarnessAssertionError):
        ssa._assert_error_code({"ok": False}, "DIRTY_CHECKOUT", "ctx")


def test_parse_envelope_rejects_empty_stdout() -> None:
    import subprocess

    result = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with pytest.raises(ssa.HarnessAssertionError, match="nothing"):
        ssa._parse_envelope(result)


def test_parse_envelope_rejects_non_json_last_line() -> None:
    import subprocess

    result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="not json at all", stderr=""
    )
    with pytest.raises(ssa.HarnessAssertionError, match="not valid JSON"):
        ssa._parse_envelope(result)


def test_parse_envelope_rejects_non_envelope_json() -> None:
    import subprocess

    result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps({"record_type": "not-an-envelope"}), stderr=""
    )
    with pytest.raises(ssa.HarnessAssertionError, match="not a codeprobe envelope"):
        ssa._parse_envelope(result)


def test_parse_envelope_accepts_real_shaped_envelope() -> None:
    import subprocess

    payload = {
        "record_type": "envelope",
        "ok": True,
        "command": "mine",
        "data": {"task_count": 5},
    }
    result = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps(payload) + "\n", stderr=""
    )
    envelope = ssa._parse_envelope(result)
    assert envelope["data"]["task_count"] == 5


# ---------------------------------------------------------------------------
# split_disjoint_results: pure validation (no codeprobe import required for
# these two checks — they raise before touching the results files at all)
# ---------------------------------------------------------------------------

sdr = _load_module("e2e_split_disjoint_results", "split_disjoint_results.py")


def test_split_disjoint_results_rejects_overlapping_keep_sets(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="disjoint"):
        sdr.split_disjoint_results(tmp_path, "armA", {"t1", "t2"}, "armB", {"t2", "t3"})
