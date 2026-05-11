"""Tests for the typed ``hide_local_source`` field (codeprobe-2nw2.4).

The field carries three string values — ``"off"``, ``"hide"``,
``"scaffold"`` — and accepts legacy boolean shapes from older
``experiment.json`` files for back-compat.

Surface covered:

* CLI ``experiment add-config --help`` exposes all three choices.
* ``codeprobe.config.loader._coerce_hide_local_source`` maps legacy
  boolean values to the new strings, passes valid strings through,
  rejects unknown strings and non-bool non-strings.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli import main as cli
from codeprobe.config.loader import _coerce_hide_local_source
from codeprobe.core.experiment import load_experiment


def test_help_lists_three_modes() -> None:
    """``experiment add-config --help`` advertises off/hide/scaffold."""
    runner = CliRunner()
    result = runner.invoke(cli, ["experiment", "add-config", "--help"])
    assert result.exit_code == 0, result.output
    # The Click choice rendering surfaces every legal value in the
    # auto-generated help text.
    assert "off" in result.output
    assert "hide" in result.output
    assert "scaffold" in result.output
    assert "--hide-local-source" in result.output


def test_loader_maps_legacy_true_to_hide() -> None:
    """JSON ``"hide_local_source": true`` maps to the new ``"hide"`` mode."""
    assert _coerce_hide_local_source(True) == "hide"


def test_loader_maps_legacy_false_to_off() -> None:
    """JSON ``"hide_local_source": false`` maps to the new ``"off"`` mode."""
    assert _coerce_hide_local_source(False) == "off"


def test_loader_maps_missing_to_off() -> None:
    """A missing field (``None``) defaults to ``"off"``."""
    assert _coerce_hide_local_source(None) == "off"


@pytest.mark.parametrize("value", ["off", "hide", "scaffold"])
def test_loader_passes_through_string_values(value: str) -> None:
    """The three legal strings round-trip unchanged."""
    assert _coerce_hide_local_source(value) == value


def test_loader_rejects_invalid_string_values() -> None:
    """An unknown string raises ``ValueError`` so a typo doesn't silently
    fall back to ``"off"``."""
    with pytest.raises(ValueError, match="hide_local_source"):
        _coerce_hide_local_source("bogus")


def test_loader_rejects_non_bool_non_string_types() -> None:
    """A numeric / list value is rejected (the only legacy form we
    honour is bool; everything else is operator error)."""
    with pytest.raises(ValueError, match="hide_local_source"):
        _coerce_hide_local_source(42)


def test_load_experiment_round_trips_scaffold(tmp_path: Path) -> None:
    """``experiment.json`` with ``"hide_local_source": "scaffold"`` round-
    trips through ``load_experiment`` as the typed enum value."""
    exp = {
        "name": "scaffold-roundtrip",
        "description": "",
        "tasks_dir": "tasks",
        "configs": [
            {
                "label": "with-scaffold",
                "agent": "claude",
                "hide_local_source": "scaffold",
            }
        ],
    }
    (tmp_path / "experiment.json").write_text(json.dumps(exp))
    (tmp_path / "tasks").mkdir()

    loaded = load_experiment(tmp_path)
    assert len(loaded.configs) == 1
    assert loaded.configs[0].hide_local_source == "scaffold"


def test_load_experiment_maps_legacy_bool_true(tmp_path: Path) -> None:
    """An ``experiment.json`` written under codeprobe-jf28 with a bool
    ``hide_local_source: true`` keeps working — loaded as ``"hide"``."""
    exp = {
        "name": "legacy-bool",
        "description": "",
        "tasks_dir": "tasks",
        "configs": [
            {
                "label": "with-hide",
                "agent": "claude",
                "hide_local_source": True,
            }
        ],
    }
    (tmp_path / "experiment.json").write_text(json.dumps(exp))
    (tmp_path / "tasks").mkdir()

    loaded = load_experiment(tmp_path)
    assert loaded.configs[0].hide_local_source == "hide"


def test_load_experiment_rejects_invalid_mode(tmp_path: Path) -> None:
    """Invalid string in ``experiment.json`` surfaces as a load-time
    ``ValueError`` rather than silently coercing."""
    exp = {
        "name": "bad-mode",
        "description": "",
        "tasks_dir": "tasks",
        "configs": [
            {
                "label": "bad",
                "agent": "claude",
                "hide_local_source": "bogus",
            }
        ],
    }
    (tmp_path / "experiment.json").write_text(json.dumps(exp))
    (tmp_path / "tasks").mkdir()

    with pytest.raises(ValueError, match="hide_local_source"):
        load_experiment(tmp_path)
