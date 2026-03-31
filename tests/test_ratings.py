"""Tests for codeprobe.ratings.collector — data models and JSONL I/O."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeprobe.ratings.collector import (
    ConfigSnapshot,
    DimensionStats,
    RatingRecord,
    load_ratings,
    record_rating,
    summarize,
)

# ---------------------------------------------------------------------------
# ConfigSnapshot
# ---------------------------------------------------------------------------


class TestConfigSnapshot:
    def test_frozen(self):
        snap = ConfigSnapshot(model="claude-sonnet-4-6")
        with pytest.raises(AttributeError):
            snap.model = "other"  # type: ignore[misc]

    def test_defaults(self):
        snap = ConfigSnapshot()
        assert snap.model is None
        assert snap.mcps == ()
        assert snap.skills == ()

    def test_roundtrip_dict(self):
        snap = ConfigSnapshot(
            model="claude-sonnet-4-6",
            mcps=("context7", "exa"),
            skills=("python-patterns",),
        )
        d = snap.to_dict()
        assert d["model"] == "claude-sonnet-4-6"
        assert d["mcps"] == ["context7", "exa"]
        assert d["skills"] == ["python-patterns"]

        restored = ConfigSnapshot.from_dict(d)
        assert restored == snap

    def test_from_dict_missing_keys(self):
        snap = ConfigSnapshot.from_dict({})
        assert snap.model is None
        assert snap.mcps == ()
        assert snap.skills == ()


# ---------------------------------------------------------------------------
# RatingRecord
# ---------------------------------------------------------------------------


class TestRatingRecord:
    def test_frozen(self):
        rec = RatingRecord(
            ts="2026-03-31T00:00:00+00:00",
            rating=4,
            config=ConfigSnapshot(),
        )
        with pytest.raises(AttributeError):
            rec.rating = 5  # type: ignore[misc]

    def test_roundtrip_dict(self):
        config = ConfigSnapshot(model="opus", mcps=("exa",))
        rec = RatingRecord(
            ts="2026-03-31T12:00:00+00:00",
            rating=5,
            config=config,
            task_type="bugfix",
            duration_s=120.5,
            tool_calls=42,
        )
        d = rec.to_dict()
        restored = RatingRecord.from_dict(d)
        assert restored == rec

    def test_from_dict_partial(self):
        rec = RatingRecord.from_dict({"rating": 3})
        assert rec.rating == 3
        assert rec.config == ConfigSnapshot()
        assert rec.task_type == ""
        assert rec.duration_s is None
        assert rec.tool_calls is None


# ---------------------------------------------------------------------------
# JSONL I/O: record_rating + load_ratings
# ---------------------------------------------------------------------------


class TestJsonlIO:
    def test_record_and_load(self, tmp_path: Path):
        path = tmp_path / "ratings.jsonl"
        config = ConfigSnapshot(model="sonnet")

        rec1 = record_rating(3, config_snapshot=config, path=path)
        rec2 = record_rating(5, config_snapshot=config, path=path)

        loaded = load_ratings(path)
        assert len(loaded) == 2
        assert loaded[0].rating == 3
        assert loaded[1].rating == 5

    def test_rating_validation(self, tmp_path: Path):
        path = tmp_path / "ratings.jsonl"
        with pytest.raises(ValueError, match="1-5"):
            record_rating(0, path=path)
        with pytest.raises(ValueError, match="1-5"):
            record_rating(6, path=path)

    def test_load_empty_file(self, tmp_path: Path):
        path = tmp_path / "ratings.jsonl"
        path.touch()
        assert load_ratings(path) == []

    def test_load_missing_file(self, tmp_path: Path):
        path = tmp_path / "nonexistent.jsonl"
        assert load_ratings(path) == []

    def test_load_skips_malformed_lines(self, tmp_path: Path):
        path = tmp_path / "ratings.jsonl"
        good = json.dumps({"ts": "t", "rating": 4, "config": {"model": "x"}})
        path.write_text(f"{good}\nNOT_JSON\n\n{good}\n")
        loaded = load_ratings(path)
        assert len(loaded) == 2

    def test_creates_parent_dirs(self, tmp_path: Path):
        path = tmp_path / "sub" / "dir" / "ratings.jsonl"
        record_rating(4, config_snapshot=ConfigSnapshot(), path=path)
        assert path.is_file()

    def test_session_metadata(self, tmp_path: Path):
        path = tmp_path / "ratings.jsonl"
        rec = record_rating(
            4,
            config_snapshot=ConfigSnapshot(model="opus"),
            session_metadata={
                "task_type": "feature",
                "duration_s": 300.0,
                "tool_calls": 15,
            },
            path=path,
        )
        assert rec.task_type == "feature"
        assert rec.duration_s == 300.0
        assert rec.tool_calls == 15

    def test_atomic_append_file_locking(self, tmp_path: Path):
        """Multiple sequential writes don't corrupt the file."""
        path = tmp_path / "ratings.jsonl"
        config = ConfigSnapshot(model="test")
        for i in range(10):
            record_rating((i % 5) + 1, config_snapshot=config, path=path)
        loaded = load_ratings(path)
        assert len(loaded) == 10


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------


class TestSummarize:
    def _make_records(self, specs: list[tuple[str, int]]) -> list[RatingRecord]:
        """Create RatingRecords from (model, rating) pairs."""
        return [
            RatingRecord(
                ts=f"2026-03-31T{i:02d}:00:00+00:00",
                rating=rating,
                config=ConfigSnapshot(model=model),
            )
            for i, (model, rating) in enumerate(specs)
        ]

    def test_empty(self):
        assert summarize([]) == {}

    def test_single_model(self):
        records = self._make_records([("opus", 5), ("opus", 3)])
        result = summarize(records)
        assert "model" in result
        stats = result["model"]
        assert len(stats) == 1
        assert stats[0].value == "opus"
        assert stats[0].mean == 4.0
        assert stats[0].count == 2

    def test_multiple_models_sorted_by_mean(self):
        records = self._make_records(
            [
                ("opus", 5),
                ("opus", 4),
                ("sonnet", 3),
                ("sonnet", 2),
            ]
        )
        result = summarize(records)
        models = result["model"]
        assert models[0].value == "opus"
        assert models[1].value == "sonnet"
        assert models[0].mean > models[1].mean

    def test_stdev_none_for_single_sample(self):
        records = self._make_records([("opus", 5)])
        result = summarize(records)
        assert result["model"][0].stdev is None

    def test_dimension_stats_frozen(self):
        ds = DimensionStats(
            dimension="model",
            value="opus",
            count=1,
            mean=5.0,
            stdev=None,
            median=5.0,
        )
        with pytest.raises(AttributeError):
            ds.mean = 3.0  # type: ignore[misc]

    def test_task_type_dimension(self):
        records = [
            RatingRecord(
                ts="2026-03-31T00:00:00+00:00",
                rating=4,
                config=ConfigSnapshot(model="opus"),
                task_type="bugfix",
            ),
            RatingRecord(
                ts="2026-03-31T01:00:00+00:00",
                rating=2,
                config=ConfigSnapshot(model="opus"),
                task_type="feature",
            ),
        ]
        result = summarize(records)
        assert "task_type" in result
        task_types = {s.value: s for s in result["task_type"]}
        assert "bugfix" in task_types
        assert "feature" in task_types
