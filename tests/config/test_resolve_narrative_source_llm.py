"""Tests for the LLM-assisted narrative-source resolver (PRD §13-T4).

Exercises :func:`codeprobe.config.defaults.resolve_narrative_source` along
three paths:

1. LLM path — ``llm_available()`` returns True, ``call_llm`` returns a
   valid JSON payload. Source tag is ``'llm'`` and the model's choice is
   honoured.
2. Offline fallback — caller passes ``offline=True``. Source tag is
   ``'offline-fallback'`` and the deterministic priority rule is used.
3. LLM-unavailable fallback — ``llm_available()`` returns False OR the
   call raises / returns garbage. Source tag is ``'llm-unavailable'`` (or
   ``'offline-fallback'``) and the deterministic priority is used.

The T4 stress-test fixtures in
``tests/cli/test_bare_invocation_matrix.py`` (``auto-squash-pr-heavy`` and
``commits-only``) are also exercised here at the resolver layer so any
regression shows up even if the matrix test is xfailed for its <20%
threshold.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeprobe.config.defaults import (
    PrescriptiveError,
    RepoShape,
    resolve_narrative_source,
)
from codeprobe.core.llm import LLMExecutionError, LLMRequest, LLMResponse

# ---------------------------------------------------------------------------
# Stub backend registration helpers
# ---------------------------------------------------------------------------


def _patch_llm(
    monkeypatch: pytest.MonkeyPatch,
    *,
    available: bool,
    response_text: str = "",
    raises: Exception | None = None,
) -> list[LLMRequest]:
    """Patch core.llm.llm_available + call_llm, return recorded requests."""
    captured: list[LLMRequest] = []

    def _fake_available() -> bool:
        return available

    def _fake_call(request: LLMRequest) -> LLMResponse:
        captured.append(request)
        if raises is not None:
            raise raises
        return LLMResponse(
            text=response_text,
            input_tokens=10,
            output_tokens=5,
            model=request.model,
            backend="stub",
        )

    monkeypatch.setattr("codeprobe.core.llm.llm_available", _fake_available)
    monkeypatch.setattr("codeprobe.core.llm.call_llm", _fake_call)
    return captured


# ---------------------------------------------------------------------------
# LLM path
# ---------------------------------------------------------------------------


def test_llm_path_honors_model_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = RepoShape(
        repo_path=tmp_path,
        has_merged_prs=True,
        commit_count=200,
        has_rfcs=True,
        pr_density=0.3,
    )
    requests = _patch_llm(
        monkeypatch,
        available=True,
        response_text=(
            '{"selected_source": "commits", "confidence": 0.82, '
            '"source": "model"}'
        ),
    )

    value, source = resolve_narrative_source(shape)

    assert value == ("commits",)
    assert source == "llm"
    assert len(requests) == 1
    prompt = requests[0].prompt
    assert "has_merged_prs" in prompt
    assert "commit_count" in prompt
    assert "pr_density" in prompt


def test_llm_path_picks_rfcs_over_priority_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model can pick rfcs even when merged PRs are available — which
    is exactly the T4 auto-squash-pr-heavy stress case."""
    shape = RepoShape(
        repo_path=tmp_path,
        has_merged_prs=True,
        commit_count=1000,
        has_rfcs=True,
        pr_density=0.9,
    )
    _patch_llm(
        monkeypatch,
        available=True,
        response_text=(
            '{"selected_source":"rfcs","confidence":0.7,"source":"model"}'
        ),
    )

    value, source = resolve_narrative_source(shape)
    assert value == ("rfcs",)
    assert source == "llm"


def test_llm_path_rejects_unavailable_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the model picks a source with no signal, fall back silently."""
    shape = RepoShape(
        repo_path=tmp_path,
        has_merged_prs=False,
        commit_count=5,  # only commits is available
        has_rfcs=False,
    )
    _patch_llm(
        monkeypatch,
        available=True,
        response_text=(
            # "rfcs" is not available
            '{"selected_source":"rfcs","confidence":0.9,"source":"model"}'
        ),
    )

    value, source = resolve_narrative_source(shape)
    assert value == ("commits",)
    assert source == "llm-unavailable"


def test_llm_path_rejects_unknown_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = RepoShape(
        repo_path=tmp_path,
        has_merged_prs=True,
        commit_count=10,
    )
    _patch_llm(
        monkeypatch,
        available=True,
        response_text=(
            '{"selected_source":"github-issues","confidence":0.5,'
            '"source":"model"}'
        ),
    )

    value, source = resolve_narrative_source(shape)
    assert value == ("pr",)
    assert source == "llm-unavailable"


def test_llm_path_parses_response_with_surrounding_prose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = RepoShape(
        repo_path=tmp_path,
        has_merged_prs=True,
        commit_count=10,
    )
    _patch_llm(
        monkeypatch,
        available=True,
        response_text=(
            "I think the answer is:\n"
            '{"selected_source":"pr","confidence":0.9,"source":"model"}\n'
            "Hope that helps!"
        ),
    )

    value, source = resolve_narrative_source(shape)
    assert value == ("pr",)
    assert source == "llm"


def test_llm_path_handles_malformed_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = RepoShape(
        repo_path=tmp_path,
        has_merged_prs=True,
        commit_count=10,
    )
    _patch_llm(
        monkeypatch,
        available=True,
        response_text="this is not json, sorry",
    )

    value, source = resolve_narrative_source(shape)
    assert value == ("pr",)
    assert source == "llm-unavailable"


def test_llm_path_handles_call_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = RepoShape(
        repo_path=tmp_path,
        has_merged_prs=False,
        commit_count=7,
    )
    _patch_llm(
        monkeypatch,
        available=True,
        raises=LLMExecutionError("API timeout"),
    )

    value, source = resolve_narrative_source(shape)
    assert value == ("commits",)
    assert source == "llm-unavailable"


# ---------------------------------------------------------------------------
# Offline fallback
# ---------------------------------------------------------------------------


def test_offline_flag_skips_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = RepoShape(
        repo_path=tmp_path,
        has_merged_prs=True,
        commit_count=10,
    )
    captured = _patch_llm(
        monkeypatch,
        available=True,
        response_text=(
            '{"selected_source":"commits","confidence":1.0,"source":"model"}'
        ),
    )

    value, source = resolve_narrative_source(shape, offline=True)

    assert value == ("pr",)
    assert source == "offline-fallback"
    assert captured == []  # the LLM was never called


def test_unavailable_backend_falls_back_to_priority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = RepoShape(
        repo_path=tmp_path,
        has_merged_prs=False,
        commit_count=0,
        has_rfcs=True,
    )
    captured = _patch_llm(monkeypatch, available=False)

    value, source = resolve_narrative_source(shape)

    assert value == ("rfcs",)
    assert source == "offline-fallback"
    assert captured == []


# ---------------------------------------------------------------------------
# Undetectable path — should never consult LLM
# ---------------------------------------------------------------------------


def test_undetectable_raises_before_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = RepoShape(repo_path=tmp_path)
    captured = _patch_llm(monkeypatch, available=True, response_text="")

    with pytest.raises(PrescriptiveError) as exc_info:
        resolve_narrative_source(shape)

    assert exc_info.value.code == "NARRATIVE_SOURCE_UNDETECTABLE"
    assert captured == []  # no LLM call on an undetectable repo


# ---------------------------------------------------------------------------
# T4 stress-test fixtures (deterministic fallback must still be correct)
# ---------------------------------------------------------------------------


def test_t4_auto_squash_pr_heavy_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """100+ auto-squash PRs are seen as commits (no merge commits) and RFCs
    are present; deterministic fallback picks commits."""
    shape = RepoShape(
        repo_path=tmp_path,
        has_merged_prs=False,  # auto-squash → no merge commits
        commit_count=120,
        has_rfcs=True,
        pr_density=0.0,
    )
    _patch_llm(monkeypatch, available=False)

    value, source = resolve_narrative_source(shape)
    assert value == ("commits",)
    assert source == "offline-fallback"


def test_t4_commits_only_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shape = RepoShape(
        repo_path=tmp_path,
        has_merged_prs=False,
        commit_count=1000,
        has_rfcs=False,
    )
    _patch_llm(monkeypatch, available=False)

    value, source = resolve_narrative_source(shape)
    assert value == ("commits",)
    assert source == "offline-fallback"
