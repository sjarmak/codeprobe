"""Unit tests for the wrong-checkout guard in conftest.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import wrong_checkout_message


@pytest.mark.parametrize(
    ("imported", "expected"),
    [
        pytest.param(
            Path("/repo/src/codeprobe"),
            Path("/repo/src/codeprobe"),
            id="identical",
        ),
        pytest.param(
            Path("/repo/worktrees/wt1/src/../src/codeprobe"),
            Path("/repo/worktrees/wt1/src/codeprobe"),
            id="equal_unresolved",
        ),
    ],
)
def test_wrong_checkout_message_none_when_paths_match(
    imported: Path,
    expected: Path,
) -> None:
    assert wrong_checkout_message(imported, expected) is None


def test_wrong_checkout_message_set_when_paths_differ() -> None:
    imported = Path("/home/ds/projects/codeprobe/src/codeprobe")
    expected = Path("/home/ds/projects/codeprobe/worktrees/wt1/src/codeprobe")

    message = wrong_checkout_message(imported, expected)

    assert message is not None
    assert str(imported) in message
    assert str(expected) in message
    assert "uv sync --extra dev" in message


def test_wrong_checkout_message_set_when_imported_is_none() -> None:
    expected = Path("/home/ds/projects/codeprobe/worktrees/wt1/src/codeprobe")

    message = wrong_checkout_message(None, expected)

    assert message is not None
    assert str(expected) in message
    assert "uv sync --extra dev" in message
