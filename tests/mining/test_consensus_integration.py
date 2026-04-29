"""Integration tests for consensus mining: end-to-end task generation.

These tests stub the symbol-discovery step and the backend resolvers so the
mining pipeline runs without git, ripgrep, Sourcegraph, or Go installed.
The goal is to exercise the wiring from
``_mine_symbol_reference_tasks`` → ``ConsensusConfig`` → ``QuarantinedCandidate``
plus the writer that drops divergence reports under ``tasks_quarantined/``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from codeprobe.mining import consensus, org_scale
from codeprobe.mining.consensus import BackendResult
from codeprobe.mining.org_scale import (
    ConsensusConfig,
    QuarantinedCandidate,
    _mine_symbol_reference_tasks,
)
from codeprobe.mining.writer import write_quarantined_task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_backends(
    monkeypatch: pytest.MonkeyPatch,
    *,
    by_symbol: dict[str, dict[str, set[str]]],
) -> None:
    """Stub the three backend resolvers with deterministic per-symbol output.

    ``by_symbol`` maps a symbol name to a per-backend file set (with keys
    "grep", "ast", "sourcegraph"). Symbols absent from the map produce an
    empty set for that backend (i.e. the backend "found nothing").
    """

    def _lookup(symbol: str, backend: str) -> set[str]:
        return by_symbol.get(symbol, {}).get(backend, set())

    def _fake_grep(symbol: str, repo_paths: Any) -> BackendResult:
        return BackendResult(
            backend="grep", files=frozenset(_lookup(symbol, "grep"))
        )

    def _fake_ast(
        symbol: str, repo_paths: Any, *, defining_file: str = ""
    ) -> BackendResult:
        return BackendResult(
            backend="ast", files=frozenset(_lookup(symbol, "ast"))
        )

    def _fake_sg(
        symbol: str,
        repo_paths: Any,
        *,
        defining_file: str,
        sg_repo: str,
        sg_url: str = "",
    ) -> BackendResult:
        return BackendResult(
            backend="sourcegraph",
            files=frozenset(_lookup(symbol, "sourcegraph")),
        )

    monkeypatch.setattr(consensus, "_run_grep_backend", _fake_grep)
    monkeypatch.setattr(consensus, "_run_ast_backend", _fake_ast)
    monkeypatch.setattr(consensus, "_run_sourcegraph_backend", _fake_sg)


# ---------------------------------------------------------------------------
# _mine_symbol_reference_tasks under consensus
# ---------------------------------------------------------------------------


def test_consensus_ships_concordant_quarantines_divergent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stub symbol discovery so the test does not need a real repo.
    monkeypatch.setattr(
        org_scale,
        "discover_reference_targets",
        lambda repo_paths, tracked_files, language: [
            (
                "ConcordantSym",
                "pkg/foo.go",
                frozenset({"caller_a.go", "caller_b.go"}),
            ),
            (
                "DivergentSym",
                "pkg/bar.go",
                frozenset({"caller_x.go"}),
            ),
        ],
    )

    # Concordant symbol: every backend agrees on two files
    # Divergent symbol: each backend picks an entirely different file
    _stub_backends(
        monkeypatch,
        by_symbol={
            "ConcordantSym": {
                "grep": {"caller_a.go", "caller_b.go"},
                "ast": {"caller_a.go", "caller_b.go"},
                "sourcegraph": {"caller_a.go", "caller_b.go"},
            },
            "DivergentSym": {
                "grep": {"way_off_a.go"},
                "ast": {"way_off_b.go"},
                "sourcegraph": {"way_off_c.go"},
            },
        },
    )

    config = ConsensusConfig(
        backends=("grep", "ast", "sourcegraph"),
        threshold=0.8,
        mode="intersection",
    )
    quarantined: list[QuarantinedCandidate] = []
    tasks = _mine_symbol_reference_tasks(
        repo_paths=[tmp_path],
        tracked_files=frozenset(),
        language="go",
        commit_sha="deadbeef" * 5,
        consensus_config=config,
        quarantined_out=quarantined,
    )

    # ConcordantSym ships, DivergentSym goes to quarantine.
    assert len(tasks) == 1
    assert tasks[0].metadata.category == "symbol-reference-trace"
    assert "ConcordantSym" in tasks[0].metadata.issue_title
    # Ground truth file set is the intersection — all 3 backends agreed.
    assert set(tasks[0].verification.oracle_answer) == {
        "caller_a.go",
        "caller_b.go",
    }

    assert len(quarantined) == 1
    cand = quarantined[0]
    assert cand.symbol == "DivergentSym"
    assert cand.family == "symbol-reference-trace"
    assert cand.divergence_report["decision"] == "quarantined"


def test_consensus_default_mode_is_intersection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        org_scale,
        "discover_reference_targets",
        lambda repo_paths, tracked_files, language: [
            ("Sym", "pkg/foo.go", frozenset({"a.go", "b.go", "c.go"})),
        ],
    )
    # Two backends fully agree; sourcegraph adds a noise file.
    _stub_backends(
        monkeypatch,
        by_symbol={
            "Sym": {
                "grep": {"a.go", "b.go"},
                "ast": {"a.go", "b.go"},
                "sourcegraph": {"a.go", "b.go", "noise.go"},
            },
        },
    )
    config = ConsensusConfig(
        backends=("grep", "ast", "sourcegraph"),
        threshold=0.5,
        mode="intersection",
    )
    quarantined: list[QuarantinedCandidate] = []
    tasks = _mine_symbol_reference_tasks(
        repo_paths=[tmp_path],
        tracked_files=frozenset(),
        language="go",
        commit_sha="abc" * 13,
        consensus_config=config,
        quarantined_out=quarantined,
    )
    assert len(tasks) == 1
    # Intersection drops "noise.go" because grep/ast didn't see it.
    assert set(tasks[0].verification.oracle_answer) == {"a.go", "b.go"}


def test_consensus_union_mode_routes_singleton_backend_files_through_curator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier-2 (single-backend) files are no longer kept just because mode='union'.

    The oracle curator (codeprobe-zat9) replaces the per-file mode-based
    decision: files seen by >= 2 backends are kept as ``required``;
    files seen by exactly one backend require an LLM keep-vote. With no
    LLM available in the test environment, ``extra.go`` (only on
    Sourcegraph) is conservatively quarantined.
    """
    monkeypatch.setattr(
        org_scale,
        "discover_reference_targets",
        lambda repo_paths, tracked_files, language: [
            ("Sym", "pkg/foo.go", frozenset()),
        ],
    )
    _stub_backends(
        monkeypatch,
        by_symbol={
            "Sym": {
                "grep": {"a.go", "b.go"},
                "ast": {"a.go", "b.go"},
                "sourcegraph": {"a.go", "b.go", "extra.go"},
            },
        },
    )
    config = ConsensusConfig(
        backends=("grep", "ast", "sourcegraph"),
        threshold=0.5,
        mode="union",
    )
    quarantined: list[QuarantinedCandidate] = []
    tasks = _mine_symbol_reference_tasks(
        repo_paths=[tmp_path],
        tracked_files=frozenset(),
        language="go",
        commit_sha="xyz" * 13,
        consensus_config=config,
        quarantined_out=quarantined,
        no_llm=True,
    )
    assert len(tasks) == 1
    # extra.go is curator-quarantined because only one backend saw it
    # and the LLM curator was disabled — keep only the 2+ backend agreement.
    assert set(tasks[0].verification.oracle_answer) == {
        "a.go",
        "b.go",
    }
    # The curator reports which backends contributed the kept items.
    # All three backends found a.go/b.go, so all three are in the consensus.
    assert set(
        tasks[0].metadata.oracle_backends_consensus
    ) == {"ast", "grep", "sourcegraph"}


# ---------------------------------------------------------------------------
# write_quarantined_task — schema + filesystem layout
# ---------------------------------------------------------------------------


def test_write_quarantined_task_emits_three_files(tmp_path: Path) -> None:
    cand = QuarantinedCandidate(
        task_id="abc12345",
        family="symbol-reference-trace",
        repo="myrepo",
        symbol="MySym",
        defining_file="pkg/foo.go",
        instruction_title="Find references to MySym in myrepo",
        instruction_body="Find all files that reference `MySym`.",
        divergence_report={
            "schema_version": "consensus.v1",
            "symbol": "MySym",
            "decision": "quarantined",
            "threshold": 0.8,
            "mode": "intersection",
            "backend_results": [],
            "pair_metrics": [],
            "consensus_files": [],
        },
    )
    base = tmp_path / "tasks_quarantined"
    base.mkdir()
    out = write_quarantined_task(
        task_id=cand.task_id,
        family=cand.family,
        repo=cand.repo,
        symbol=cand.symbol,
        defining_file=cand.defining_file,
        instruction_title=cand.instruction_title,
        instruction_body=cand.instruction_body,
        divergence_report=cand.divergence_report,
        base_dir=base,
    )

    assert out == base / "abc12345"
    assert (out / "instruction.md").is_file()
    assert (out / "divergence_report.json").is_file()
    assert (out / "metadata.json").is_file()
    # Critically: no ground_truth.json — the whole point of quarantine is
    # that we couldn't agree on one.
    assert not (out / "ground_truth.json").exists()
    assert not (out / "tests").exists()

    md = json.loads((out / "metadata.json").read_text())
    assert md["status"] == "quarantined"
    assert md["symbol"] == "MySym"
    assert md["family"] == "symbol-reference-trace"

    div = json.loads((out / "divergence_report.json").read_text())
    assert div["decision"] == "quarantined"


def test_write_quarantined_task_rejects_unsafe_id(tmp_path: Path) -> None:
    base = tmp_path / "tasks_quarantined"
    base.mkdir()
    with pytest.raises(ValueError, match="Invalid task id"):
        write_quarantined_task(
            task_id="../escape",
            family="symbol-reference-trace",
            repo="r",
            symbol="s",
            defining_file="f",
            instruction_title="t",
            instruction_body="b",
            divergence_report={},
            base_dir=base,
        )


# ---------------------------------------------------------------------------
# Legacy single-backend path is preserved when consensus is disabled
# ---------------------------------------------------------------------------


def test_no_consensus_path_returns_single_backend_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With consensus_config=None, the existing _maybe_enrich path runs."""
    monkeypatch.setattr(
        org_scale,
        "discover_reference_targets",
        lambda repo_paths, tracked_files, language: [
            ("LegacySym", "pkg/foo.go", frozenset({"old_path.go"})),
        ],
    )
    monkeypatch.setattr(
        org_scale,
        "_get_sg_config",
        lambda sg_repo, *, strict=False: (False,),
    )

    quarantined: list[QuarantinedCandidate] = []
    tasks = _mine_symbol_reference_tasks(
        repo_paths=[tmp_path],
        tracked_files=frozenset(),
        language="go",
        commit_sha="abc" * 13,
        consensus_config=None,
        quarantined_out=quarantined,
    )
    # Single task, no quarantine — legacy path is unchanged.
    assert len(tasks) == 1
    assert quarantined == []
