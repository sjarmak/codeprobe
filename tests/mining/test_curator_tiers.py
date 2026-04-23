"""R3: ZFC-compliant tier assigner for ground-truth files.

Covers ``assign_ground_truth_tiers`` and ``assign_mcp_family_tiers`` —
PR-diff → required, 1-hop → supplementary, 2-hop → context.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from codeprobe.core.llm import LLMError, LLMResponse
from codeprobe.mining.curator_tiers import (
    assign_ground_truth_tiers,
    assign_mcp_family_tiers,
)
from codeprobe.mining.org_scale_families import TaskFamily

_FAMILY = TaskFamily(
    name="test-family",
    description="Test family for the tier-assigner unit tests.",
    glob_patterns=("**/*.py",),
    content_patterns=(r"foo",),
)


# ---------------------------------------------------------------------------
# Heuristic (use_llm=False) — no LLM call expected
# ---------------------------------------------------------------------------


class TestHeuristicTierAssignment:
    def test_pr_diff_files_required(self) -> None:
        tiers = assign_ground_truth_tiers(
            ground_truth_files={"a.py", "b.py"},
            pr_diff_files={"a.py", "b.py"},
            reference_graph={},
            use_llm=False,
        )
        assert tiers["a.py"] == "required"
        assert tiers["b.py"] == "required"

    def test_one_hop_supplementary(self) -> None:
        tiers = assign_ground_truth_tiers(
            ground_truth_files={"a.py", "b.py"},
            pr_diff_files={"a.py"},
            reference_graph={"a.py": ["b.py"]},
            use_llm=False,
        )
        assert tiers["a.py"] == "required"
        assert tiers["b.py"] == "supplementary"

    def test_two_hop_context(self) -> None:
        tiers = assign_ground_truth_tiers(
            ground_truth_files={"a.py", "b.py", "c.py"},
            pr_diff_files={"a.py"},
            reference_graph={"a.py": ["b.py"], "b.py": ["c.py"]},
            use_llm=False,
        )
        assert tiers["a.py"] == "required"
        assert tiers["b.py"] == "supplementary"
        assert tiers["c.py"] == "context"

    def test_disconnected_defaults_to_context(self) -> None:
        tiers = assign_ground_truth_tiers(
            ground_truth_files={"a.py", "orphan.py"},
            pr_diff_files={"a.py"},
            reference_graph={"a.py": []},
            use_llm=False,
        )
        assert tiers["a.py"] == "required"
        assert tiers["orphan.py"] == "context"

    def test_empty_ground_truth(self) -> None:
        assert assign_ground_truth_tiers(
            ground_truth_files=set(),
            pr_diff_files=set(),
            use_llm=False,
        ) == {}

    def test_non_homogeneous_tiers_with_five_files(self) -> None:
        """PRD acceptance criterion 9: ≥5 GT files → non-homogeneous dict."""
        graph = {
            "seed.py": ["hop1_a.py", "hop1_b.py"],
            "hop1_a.py": ["hop2.py"],
        }
        tiers = assign_ground_truth_tiers(
            ground_truth_files={
                "seed.py",
                "hop1_a.py",
                "hop1_b.py",
                "hop2.py",
                "isolated.py",
            },
            pr_diff_files={"seed.py"},
            reference_graph=graph,
            use_llm=False,
        )
        unique_tiers = set(tiers.values())
        assert len(unique_tiers) > 1
        # At least one tier that is NOT "required" (PRD criterion 9)
        assert unique_tiers - {"required"}, (
            f"expected at least one non-required tier, got {tiers!r}"
        )


# ---------------------------------------------------------------------------
# LLM refinement path — use_llm=True
# ---------------------------------------------------------------------------


class TestLLMRefinement:
    @patch("codeprobe.mining.curator_tiers.llm_available", return_value=True)
    @patch("codeprobe.mining.curator_tiers.call_claude")
    def test_llm_invoked_before_any_tier_returned(
        self,
        mock_call: object,
        mock_avail: object,
    ) -> None:
        """PRD: call_claude must fire in the control flow before any
        string-literal tier reaches the caller when use_llm=True.
        """
        mock_call.return_value = LLMResponse(
            text=json.dumps({"a.py": "required", "b.py": "supplementary"})
        )
        assign_ground_truth_tiers(
            ground_truth_files={"a.py", "b.py"},
            pr_diff_files={"a.py"},
            reference_graph={"a.py": ["b.py"]},
            family=_FAMILY,
            use_llm=True,
        )
        assert mock_call.called, "call_claude must be invoked when use_llm=True"

    @patch("codeprobe.mining.curator_tiers.llm_available", return_value=True)
    @patch("codeprobe.mining.curator_tiers.call_claude")
    def test_llm_overrides_heuristic_when_valid(
        self,
        mock_call: object,
        mock_avail: object,
    ) -> None:
        """LLM is allowed to re-tier a heuristic result within the valid set."""
        mock_call.return_value = LLMResponse(
            text=json.dumps({"a.py": "context", "b.py": "required"})
        )
        tiers = assign_ground_truth_tiers(
            ground_truth_files={"a.py", "b.py"},
            pr_diff_files={"a.py"},
            reference_graph={"a.py": ["b.py"]},
            family=_FAMILY,
            use_llm=True,
        )
        assert tiers["a.py"] == "context"
        assert tiers["b.py"] == "required"

    @patch("codeprobe.mining.curator_tiers.llm_available", return_value=True)
    @patch("codeprobe.mining.curator_tiers.call_claude")
    def test_llm_error_falls_back_to_heuristic(
        self,
        mock_call: object,
        mock_avail: object,
    ) -> None:
        mock_call.side_effect = LLMError("API down")
        tiers = assign_ground_truth_tiers(
            ground_truth_files={"a.py", "b.py"},
            pr_diff_files={"a.py"},
            reference_graph={"a.py": ["b.py"]},
            use_llm=True,
        )
        assert tiers == {"a.py": "required", "b.py": "supplementary"}

    @patch("codeprobe.mining.curator_tiers.llm_available", return_value=True)
    @patch("codeprobe.mining.curator_tiers.call_claude")
    def test_invalid_llm_tier_ignored(
        self,
        mock_call: object,
        mock_avail: object,
    ) -> None:
        """Out-of-vocabulary tiers from the LLM fall back to heuristic."""
        mock_call.return_value = LLMResponse(
            text=json.dumps({"a.py": "nonsense", "b.py": "supplementary"})
        )
        tiers = assign_ground_truth_tiers(
            ground_truth_files={"a.py", "b.py"},
            pr_diff_files={"a.py"},
            reference_graph={"a.py": ["b.py"]},
            use_llm=True,
        )
        # The non-sense tier for a.py is rejected by _parse_tier_response,
        # so the whole LLM response is discarded and heuristic applies.
        assert tiers["a.py"] == "required"
        assert tiers["b.py"] == "supplementary"

    @patch("codeprobe.mining.curator_tiers.llm_available", return_value=False)
    def test_llm_unavailable_falls_back_to_heuristic(
        self,
        mock_avail: object,
    ) -> None:
        tiers = assign_ground_truth_tiers(
            ground_truth_files={"a.py"},
            pr_diff_files={"a.py"},
            use_llm=True,
        )
        assert tiers == {"a.py": "required"}


# ---------------------------------------------------------------------------
# assign_mcp_family_tiers — tuple convenience used by org_scale.py miners
# ---------------------------------------------------------------------------


class TestMCPFamilyTiers:
    @patch("codeprobe.mining.curator_tiers.llm_available", return_value=False)
    def test_returns_ordered_tuple(self, mock_avail: object) -> None:
        tiers = assign_mcp_family_tiers(
            required_files={"a.py", "b.py"},
            supplementary_files={"c.py"},
        )
        assert isinstance(tiers, tuple)
        assert all(isinstance(t, tuple) and len(t) == 2 for t in tiers)
        paths = [p for p, _ in tiers]
        assert paths == ["a.py", "b.py", "c.py"]

    @patch("codeprobe.mining.curator_tiers.llm_available", return_value=False)
    def test_required_set_marked_required(self, mock_avail: object) -> None:
        tiers = assign_mcp_family_tiers(
            required_files={"a.py"},
            supplementary_files={"b.py"},
        )
        tier_map = dict(tiers)
        assert tier_map["a.py"] == "required"
        assert tier_map["b.py"] == "supplementary"

    @patch("codeprobe.mining.curator_tiers.llm_available", return_value=True)
    @patch("codeprobe.mining.curator_tiers.call_claude")
    def test_llm_invoked_before_tier_tuple(
        self,
        mock_call: object,
        mock_avail: object,
    ) -> None:
        mock_call.return_value = LLMResponse(
            text=json.dumps({"a.py": "required", "b.py": "supplementary"})
        )
        assign_mcp_family_tiers(
            required_files={"a.py"},
            supplementary_files={"b.py"},
        )
        assert mock_call.called

    @patch("codeprobe.mining.curator_tiers.llm_available", return_value=False)
    def test_empty_inputs_return_empty_tuple(self, mock_avail: object) -> None:
        assert assign_mcp_family_tiers(required_files=set()) == ()
