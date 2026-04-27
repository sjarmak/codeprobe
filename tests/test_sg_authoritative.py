"""Tests for sg_authoritative mode in _maybe_enrich.

When a mining family asks a reference question that ``sg_find_references``
is the ground truth for (e.g. symbol-reference-trace), the Sourcegraph
result should BE the oracle, not a grep+filter union. This aligns oracle
semantics with the task framing.

Change-scope-audit keeps the union+filter behavior — its framing
("blast radius") requires a broader file set than strict refs.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codeprobe.mining.org_scale import _maybe_enrich
from codeprobe.mining.sg_auth import CachedToken

_FAKE_TOKEN = CachedToken(
    access_token="test-token",
    refresh_token=None,
    expires_at=None,
    endpoint="https://demo.sourcegraph.com",
)


@pytest.fixture(autouse=True)
def _mock_auth():
    with patch(
        "codeprobe.mining.sg_auth.get_valid_token",
        return_value=_FAKE_TOKEN,
    ):
        yield


def _sg_sse_response(file_paths: list[str], repo: str):
    text_lines = []
    for fp in file_paths:
        text_lines.append(f"# {repo} – {fp}")
        text_lines.append("1: some code")
        text_lines.append("")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": "\n".join(text_lines)}]},
    }
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.iter_lines.return_value = iter([f"data: {json.dumps(payload)}"])
    return resp


def _sg_failure_response():
    resp = MagicMock()
    resp.status_code = 500
    resp.raise_for_status.side_effect = Exception("500 Server Error")
    return resp


# ---------------------------------------------------------------------------
# sg_authoritative=True — SG result IS the ground truth
# ---------------------------------------------------------------------------


def test_sg_authoritative_uses_sg_only(tmp_path: Path) -> None:
    """When SG returns a non-empty set, that's the entire ground truth —
    grep files are NOT unioned in, import filter is NOT applied."""
    repo = "github.com/example/proj"
    sg_refs = ["pkg/a.go", "pkg/b.go", "pkg/c.go"]
    # Grep found a DIFFERENT set (noisy token match including stdlib)
    grep_files = frozenset({"pkg/a.go", "noise/stdlib_only.go", "vendor/dep.go"})

    with patch("codeprobe.mining.sg_ground_truth.requests") as mock_req:
        mock_req.post.return_value = _sg_sse_response(sg_refs, repo=repo)
        files, tier_tuple = _maybe_enrich(
            sg_available=True,
            sg_repo=repo,
            symbol="MkdirAll",
            def_file="pkg/x.go",
            grep_files=grep_files,
            repo_path=tmp_path,
            language="go",
            sg_authoritative=True,
        )

    # Exactly SG's set, no grep noise leaking in
    assert files == frozenset({"pkg/a.go", "pkg/b.go", "pkg/c.go"})
    # All SG-derived → "required" tier
    tier_map = dict(tier_tuple)
    assert all(t == "required" for t in tier_map.values())
    assert set(tier_map.keys()) == files


def test_sg_authoritative_falls_back_on_api_failure(tmp_path: Path) -> None:
    """If SG fails (returns None), fall back to grep + import filter so
    mining still produces a usable task."""
    repo = "github.com/example/proj"
    # Set up repo with go.mod and a defining file so the filter can run
    (tmp_path / "go.mod").write_text("module github.com/example/proj\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "x.go").write_text("package pkg\n")
    (tmp_path / "pkg" / "sibling.go").write_text("package pkg\n")

    grep_files = frozenset({"pkg/sibling.go", "noise/stdlib_only.go"})

    with patch("codeprobe.mining.sg_ground_truth.requests") as mock_req:
        mock_req.post.return_value = _sg_failure_response()
        files, tier_tuple = _maybe_enrich(
            sg_available=True,
            sg_repo=repo,
            symbol="MkdirAll",
            def_file="pkg/x.go",
            grep_files=grep_files,
            repo_path=tmp_path,
            language="go",
            sg_authoritative=True,
        )

    # pkg/sibling.go is same-package (kept); noise/stdlib_only.go doesn't
    # exist on disk (kept conservatively by the filter)
    assert "pkg/sibling.go" in files


def test_sg_authoritative_false_preserves_union_behavior(tmp_path: Path) -> None:
    """Default behavior (change-scope-audit): SG and grep are unioned, then
    import filter runs."""
    repo = "github.com/example/proj"
    (tmp_path / "go.mod").write_text("module github.com/example/proj\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "x.go").write_text("package pkg\n")
    (tmp_path / "pkg" / "same_pkg.go").write_text("package pkg\n")

    sg_refs = ["pkg/same_pkg.go"]
    grep_files = frozenset({"pkg/same_pkg.go"})

    with patch("codeprobe.mining.sg_ground_truth.requests") as mock_req:
        mock_req.post.return_value = _sg_sse_response(sg_refs, repo=repo)
        files, _ = _maybe_enrich(
            sg_available=True,
            sg_repo=repo,
            symbol="MkdirAll",
            def_file="pkg/x.go",
            grep_files=grep_files,
            repo_path=tmp_path,
            language="go",
            # sg_authoritative defaults to False
        )

    # Same file via both sources — kept
    assert files == frozenset({"pkg/same_pkg.go"})
