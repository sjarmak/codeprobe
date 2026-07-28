"""Reusable structural validators for evidence artifacts."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from codeprobe.snapshot.evidence_models import EvidenceBundleValidationError

_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class _DuplicateJsonFieldError(ValueError):
    """Internal duplicate-field sentinel."""


def _reject_duplicate_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = tuple(key for key, _ in pairs)
    if len(frozenset(keys)) != len(keys):
        raise _DuplicateJsonFieldError
    return dict(pairs)


def error(path: str, message: str) -> EvidenceBundleValidationError:
    return EvidenceBundleValidationError(f"{path}: {message}")


def object_value(
    value: object, path: str, expected_keys: frozenset[str]
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise error(path, "must be an object")
    if not all(isinstance(key, str) for key in value):
        raise error(path, "field names must be strings")
    actual = frozenset(value)
    unexpected = sorted(actual - expected_keys)
    missing = sorted(expected_keys - actual)
    if unexpected:
        raise error(path, "contains unexpected field(s)")
    if missing:
        raise error(path, f"missing field(s): {', '.join(missing)}")
    return value


def array_value(
    value: object,
    path: str,
    *,
    maximum: int = 10_000,
) -> Sequence[object]:
    if not isinstance(value, list):
        raise error(path, "must be an array")
    if len(value) > maximum:
        raise error(path, f"must contain at most {maximum} items")
    return value


def object_array(
    value: object,
    path: str,
    expected_item_keys: frozenset[str],
    *,
    maximum: int = 10_000,
) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        object_value(item, f"{path}[{index}]", expected_item_keys)
        for index, item in enumerate(
            array_value(value, path, maximum=maximum)
        )
    )


def string_value(
    value: object,
    path: str,
    *,
    choices: Sequence[str] | None = None,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if not isinstance(value, str):
        raise error(path, "must be a string")
    if choices is not None and value not in choices:
        raise error(path, "contains an unsupported value")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise error(path, "has an invalid format")
    return value


def boolean_value(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise error(path, "must be a boolean")
    return value


def integer_value(value: object, path: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise error(path, "must be an integer")
    if value < minimum:
        raise error(path, f"must be at least {minimum}")
    return value


def number_value(
    value: object,
    path: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise error(path, "must be a number")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise error(path, "must be finite") from None
    if not math.isfinite(result):
        raise error(path, "must be finite")
    if minimum is not None and result < minimum:
        raise error(path, f"must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise error(path, f"must be at most {maximum}")
    return result


def optional_number(
    value: object,
    path: str,
    *,
    minimum: float | None = None,
) -> float | None:
    return (
        None
        if value is None
        else number_value(value, path, minimum=minimum)
    )


def digest_value(value: object, path: str) -> str:
    return string_value(value, path, pattern=_DIGEST_PATTERN)


def interval_value(
    value: object,
    path: str,
    *,
    bounded_quality: bool = False,
) -> None:
    raw = object_value(value, path, frozenset({"lower", "upper"}))
    minimum = 0.0 if bounded_quality else None
    maximum = 1.0 if bounded_quality else None
    lower = number_value(
        raw["lower"], f"{path}.lower", minimum=minimum, maximum=maximum
    )
    upper = number_value(
        raw["upper"], f"{path}.upper", minimum=minimum, maximum=maximum
    )
    if upper < lower:
        raise error(path, "upper must not be below lower")


def parse_json(
    documents: Mapping[str, str], filename: str
) -> Mapping[str, Any]:
    try:
        value = json.loads(
            documents[filename],
            object_pairs_hook=_reject_duplicate_fields,
        )
    except _DuplicateJsonFieldError as exc:
        raise error(filename, "contains duplicate field(s)") from exc
    except (ValueError, RecursionError) as exc:
        raise error(filename, "must contain valid JSON") from exc
    expected = frozenset(value) if isinstance(value, Mapping) else frozenset()
    return object_value(value, filename, expected)
