"""Tests for curator-core: protocols, data models, and merge pipeline."""

from __future__ import annotations

from pathlib import Path

import pytest

from codeprobe.mining.curator import (
    CuratedFile,
    CurationBackend,
    CurationPipeline,
    CurationResult,
    MergeConfig,
    merge_results,
)
from codeprobe.mining.org_scale_families import MIGRATION_INVENTORY, TaskFamily
from codeprobe.mining.org_scale_scanner import FamilyScanResult, PatternHit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAMILY = MIGRATION_INVENTORY


class _StubBackend:
    """A simple stub backend for testing."""

    def __init__(
        self, name: str, files: list[CuratedFile], is_available: bool = True
    ) -> None:
        self._name = name
        self._files = files
        self._is_available = is_available

    @property
    def name(self) -> str:
        return self._name

    def search(self, repos: list[Path], family: TaskFamily) -> list[CuratedFile]:
        return self._files

    def available(self) -> bool:
        return self._is_available


# ---------------------------------------------------------------------------
# merge_results: single backend passthrough
# ---------------------------------------------------------------------------


class TestMergeSingleBackend:
    def test_passthrough(self) -> None:
        files = [
            CuratedFile(path="a.py", sources=("grep",), confidence=0.9, hit_count=2),
            CuratedFile(path="b.py", sources=("grep",), confidence=0.8, hit_count=1),
        ]
        config = MergeConfig()
        result = merge_results({"grep": files}, config)

        assert len(result) == 2
        assert result[0].path == "a.py"
        assert result[0].confidence == 0.9
        assert result[0].hit_count == 2
        assert result[0].sources == ("grep",)
        assert result[1].path == "b.py"


# ---------------------------------------------------------------------------
# merge_results: multi-backend dedup
# ---------------------------------------------------------------------------


class TestMergeMultiBackend:
    def test_dedup_merges_sources_and_sums_hits(self) -> None:
        grep_files = [
            CuratedFile(
                path="shared.py", sources=("grep",), confidence=0.8, hit_count=3
            ),
        ]
        ast_files = [
            CuratedFile(
                path="shared.py", sources=("ast",), confidence=0.6, hit_count=2
            ),
        ]
        config = MergeConfig()
        result = merge_results({"grep": grep_files, "ast": ast_files}, config)

        assert len(result) == 1
        merged = result[0]
        assert merged.path == "shared.py"
        assert merged.sources == ("grep", "ast")
        assert merged.hit_count == 5  # 3 + 2
        # Weighted average: (0.8*1.0 + 0.6*1.0) / 2.0 = 0.7
        assert merged.confidence == pytest.approx(0.7)

    def test_weighted_confidence(self) -> None:
        grep_files = [CuratedFile(path="x.py", sources=("grep",), confidence=1.0)]
        ast_files = [CuratedFile(path="x.py", sources=("ast",), confidence=0.5)]
        config = MergeConfig(backend_weights={"grep": 2.0, "ast": 1.0})
        result = merge_results({"grep": grep_files, "ast": ast_files}, config)

        assert len(result) == 1
        # (1.0*2.0 + 0.5*1.0) / 3.0 = 2.5/3.0 ≈ 0.8333
        assert result[0].confidence == pytest.approx(2.5 / 3.0)


# ---------------------------------------------------------------------------
# merge_results: quorum filtering
# ---------------------------------------------------------------------------


class TestQuorumFiltering:
    def test_min_backends_drops_single_source(self) -> None:
        grep_files = [
            CuratedFile(path="both.py", sources=("grep",), confidence=0.9),
            CuratedFile(path="grep_only.py", sources=("grep",), confidence=0.9),
        ]
        ast_files = [
            CuratedFile(path="both.py", sources=("ast",), confidence=0.8),
        ]
        config = MergeConfig(min_backends=2)
        result = merge_results({"grep": grep_files, "ast": ast_files}, config)

        paths = [cf.path for cf in result]
        assert "both.py" in paths
        assert "grep_only.py" not in paths


# ---------------------------------------------------------------------------
# merge_results: confidence filtering
# ---------------------------------------------------------------------------


class TestConfidenceFiltering:
    def test_low_confidence_filtered(self) -> None:
        files = [
            CuratedFile(path="good.py", sources=("grep",), confidence=0.8),
            CuratedFile(path="bad.py", sources=("grep",), confidence=0.1),
        ]
        config = MergeConfig(min_confidence=0.3)
        result = merge_results({"grep": files}, config)

        paths = [cf.path for cf in result]
        assert "good.py" in paths
        assert "bad.py" not in paths


# ---------------------------------------------------------------------------
# merge_results: empty input
# ---------------------------------------------------------------------------


class TestMergeEmpty:
    def test_empty_dict_returns_empty(self) -> None:
        result = merge_results({}, MergeConfig())
        assert result == ()

    def test_empty_file_lists_returns_empty(self) -> None:
        result = merge_results({"grep": []}, MergeConfig())
        assert result == ()


class TestMergeExcludesVendor:
    def test_vendor_files_excluded(self) -> None:
        files = {
            "grep": [
                CuratedFile(path="src/main.go"),
                CuratedFile(path="vendor/lib/dep.go"),
            ],
            "agent": [
                CuratedFile(path="src/main.go"),
                CuratedFile(path="node_modules/pkg/mod.js"),
                CuratedFile(path="testdata/fixture.go"),
            ],
        }
        result = merge_results(files, MergeConfig())
        paths = {cf.path for cf in result}
        assert "src/main.go" in paths
        assert "vendor/lib/dep.go" not in paths
        assert "node_modules/pkg/mod.js" not in paths
        assert "testdata/fixture.go" not in paths


# ---------------------------------------------------------------------------
# CurationResult.from_scan_result
# ---------------------------------------------------------------------------


class TestFromScanResult:
    def test_round_trip(self) -> None:
        repo = Path("/tmp/test-repo")
        hits = (
            PatternHit(
                file_path="a.py",
                line_number=10,
                matched_text="@deprecated",
                pattern_used=r"@[Dd]eprecated",
            ),
            PatternHit(
                file_path="a.py",
                line_number=20,
                matched_text="@Deprecated",
                pattern_used=r"@[Dd]eprecated",
            ),
            PatternHit(
                file_path="b.py",
                line_number=5,
                matched_text="@deprecated",
                pattern_used=r"@[Dd]eprecated",
            ),
        )
        scan = FamilyScanResult(
            family=_FAMILY,
            hits=hits,
            repo_paths=(repo,),
            commit_sha="abc123",
            matched_files=frozenset({"a.py", "b.py"}),
        )

        result = CurationResult.from_scan_result(scan)

        assert result.family == _FAMILY
        assert result.repo_paths == (repo,)
        assert result.commit_shas == {"test-repo": "abc123"}
        assert result.backends_used == ("grep",)
        assert result.matched_files == frozenset({"a.py", "b.py"})
        assert len(result.files) == 2

        # Files are sorted by path.
        a_file = result.files[0]
        assert a_file.path == "a.py"
        assert a_file.tier == "required"
        assert a_file.sources == ("grep",)
        assert a_file.confidence == 1.0
        assert a_file.hit_count == 2
        assert a_file.line_matches == (10, 20)

        b_file = result.files[1]
        assert b_file.path == "b.py"
        assert b_file.hit_count == 1
        assert b_file.line_matches == (5,)

    def test_rejects_non_scan_result(self) -> None:
        with pytest.raises(TypeError, match="Expected FamilyScanResult"):
            CurationResult.from_scan_result({"not": "a scan result"})


# ---------------------------------------------------------------------------
# CurationPipeline with mock backends
# ---------------------------------------------------------------------------


class TestCurationPipeline:
    def test_runs_available_backends_and_merges(self) -> None:
        grep_files = [CuratedFile(path="found.py", sources=("grep",), confidence=0.9)]
        ast_files = [CuratedFile(path="found.py", sources=("ast",), confidence=0.7)]

        grep_backend = _StubBackend("grep", grep_files)
        ast_backend = _StubBackend("ast", ast_files)

        pipeline = CurationPipeline(backends=[grep_backend, ast_backend])
        result = pipeline.curate(
            repos=[Path("/tmp/repo")],
            family=_FAMILY,
        )

        assert isinstance(result, CurationResult)
        assert result.family == _FAMILY
        assert set(result.backends_used) == {"grep", "ast"}
        assert len(result.files) == 1
        assert result.files[0].path == "found.py"
        assert result.files[0].confidence == pytest.approx(0.8)
        assert result.matched_files == frozenset({"found.py"})

    def test_skips_unavailable_backends(self) -> None:
        available = _StubBackend("grep", [CuratedFile(path="a.py", sources=("grep",))])
        unavailable = _StubBackend(
            "ast", [CuratedFile(path="b.py", sources=("ast",))], is_available=False
        )

        pipeline = CurationPipeline(backends=[available, unavailable])
        result = pipeline.curate(repos=[Path("/tmp/repo")], family=_FAMILY)

        assert result.backends_used == ("grep",)
        assert len(result.files) == 1
        assert result.files[0].path == "a.py"

    def test_no_backends_returns_empty(self) -> None:
        pipeline = CurationPipeline(backends=[])
        result = pipeline.curate(repos=[Path("/tmp/repo")], family=_FAMILY)

        assert result.files == ()
        assert result.backends_used == ()
        assert result.matched_files == frozenset()

    def test_failing_backend_does_not_crash(self) -> None:
        class _FailingBackend:
            @property
            def name(self) -> str:
                return "broken"

            def search(
                self, repos: list[Path], family: TaskFamily
            ) -> list[CuratedFile]:
                msg = "boom"
                raise RuntimeError(msg)

            def available(self) -> bool:
                return True

        good = _StubBackend("grep", [CuratedFile(path="ok.py", sources=("grep",))])
        bad = _FailingBackend()

        pipeline = CurationPipeline(backends=[good, bad])  # type: ignore[list-item]
        result = pipeline.curate(repos=[Path("/tmp/repo")], family=_FAMILY)

        assert result.backends_used == ("grep",)
        assert len(result.files) == 1


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_stub_is_curation_backend(self) -> None:
        backend = _StubBackend("test", [])
        assert isinstance(backend, CurationBackend)
