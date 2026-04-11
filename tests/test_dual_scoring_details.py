"""Unit tests for DualScoringDetails dataclass."""

from __future__ import annotations

from codeprobe.models.experiment import CompletedTask, DualScoringDetails


def test_from_dict_full() -> None:
    """from_dict populates all fields from a full input dict."""
    data = {
        "score_direct": 0.8,
        "score_artifact": 0.6,
        "passed_direct": True,
        "passed_artifact": False,
        "scoring_policy": "min",
        "extra": {"note": "hi"},
    }
    details = DualScoringDetails.from_dict(data)

    assert details.score_direct == 0.8
    assert details.score_artifact == 0.6
    assert details.passed_direct is True
    assert details.passed_artifact is False
    assert details.scoring_policy == "min"
    assert details.extra == {"note": "hi"}


def test_from_dict_empty() -> None:
    """from_dict({}) yields sensible defaults: zero floats, False bools, '' string, {} dict."""
    details = DualScoringDetails.from_dict({})

    assert details.score_direct == 0.0
    assert details.score_artifact == 0.0
    assert details.passed_direct is False
    assert details.passed_artifact is False
    assert details.scoring_policy == ""
    assert details.extra == {}


def test_to_dict() -> None:
    """to_dict returns a dict equivalent to its source fields."""
    details = DualScoringDetails(
        score_direct=0.5,
        score_artifact=0.75,
        passed_direct=True,
        passed_artifact=True,
        scoring_policy="max",
        extra={"k": "v"},
    )
    out = details.to_dict()

    assert out == {
        "score_direct": 0.5,
        "score_artifact": 0.75,
        "passed_direct": True,
        "passed_artifact": True,
        "scoring_policy": "max",
        "extra": {"k": "v"},
    }
    assert isinstance(out, dict)


def test_round_trip() -> None:
    """from_dict(to_dict(x)) == x for a populated instance."""
    original = DualScoringDetails(
        score_direct=0.42,
        score_artifact=0.13,
        passed_direct=True,
        passed_artifact=False,
        scoring_policy="avg",
        extra={"weight": 2},
    )
    round_tripped = DualScoringDetails.from_dict(original.to_dict())
    assert round_tripped == original


def test_round_trip_empty_defaults() -> None:
    """Defaults round-trip cleanly through to_dict/from_dict."""
    original = DualScoringDetails()
    round_tripped = DualScoringDetails.from_dict(original.to_dict())
    assert round_tripped == original


def test_completed_task_scoring_details_still_dict() -> None:
    """CompletedTask.scoring_details must remain typed as a plain dict.

    Per the R10-PM critical review: do NOT change the field to
    DualScoringDetails — checkpoint serialization relies on dict on the wire.
    """
    annotation = CompletedTask.__annotations__["scoring_details"]
    # Annotation may be the `dict` type itself or a string (postponed eval).
    annotation_str = annotation if isinstance(annotation, str) else annotation.__name__
    assert "dict" in annotation_str.lower()
    assert "DualScoringDetails" not in annotation_str
