"""Shared fixtures for mining state tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def tenant_state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``~/.codeprobe/state`` into *tmp_path* for the test.

    Uses the ``CODEPROBE_STATE_ROOT`` env-var escape hatch exposed by
    :mod:`codeprobe.paths` so the real user home is never touched.
    """
    root = tmp_path / "state"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CODEPROBE_STATE_ROOT", str(root))
    return root
