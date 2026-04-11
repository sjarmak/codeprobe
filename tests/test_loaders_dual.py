"""Tests for dual-verification loader fields (u2-task-model-policy).

Covers:
- ``TaskVerification.scoring_policy`` / ``weight_direct`` / ``weight_artifact``
  defaults and TOML-driven population.
- The pre-existing bug where ``[verification].verification_mode`` in TOML was
  dropped rather than passed through to ``TaskVerification``.
"""

from __future__ import annotations

from pathlib import Path

from codeprobe.loaders import load_task
from codeprobe.models.task import TaskVerification

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "dual_task"


# -- Dataclass default tests ------------------------------------------------


class TestTaskVerificationDefaults:
    def test_new_fields_have_documented_defaults(self) -> None:
        v = TaskVerification()
        assert v.scoring_policy == ""
        assert v.weight_direct == 0.5
        assert v.weight_artifact == 0.5

    def test_dataclass_remains_frozen(self) -> None:
        v = TaskVerification()
        try:
            v.scoring_policy = "min"  # type: ignore[misc]
        except (AttributeError, Exception):  # noqa: BLE001
            return
        raise AssertionError("TaskVerification should remain frozen")


# -- Fixture-based loader tests ---------------------------------------------


class TestLoadDualTaskFixture:
    def test_fixture_loads(self) -> None:
        task = load_task(FIXTURE_DIR / "task.toml")
        assert task.id == "dual-task-001"
        assert task.repo == "org/repo"

    def test_verification_mode_populated_from_toml(self) -> None:
        """Regression: [verification].verification_mode was previously dropped."""
        task = load_task(FIXTURE_DIR / "task.toml")
        assert task.verification.verification_mode == "dual"

    def test_scoring_policy_and_weights_populate(self) -> None:
        task = load_task(FIXTURE_DIR / "task.toml")
        assert task.verification.scoring_policy == "weighted"
        assert task.verification.weight_direct == 0.3
        assert task.verification.weight_artifact == 0.7


# -- Inline TOML coverage ---------------------------------------------------


DUAL_MIN_TOML = """\
[task]
id = "dual-min-001"
repo = "org/repo"

[metadata]
name = "dual-min"

[verification]
verification_mode = "dual"
scoring_policy = "min"
"""


DUAL_WEIGHTED_TOML = """\
[task]
id = "dual-weighted-001"
repo = "org/repo"

[metadata]
name = "dual-weighted"

[verification]
verification_mode = "dual"
scoring_policy = "weighted"
weight_direct = 0.3
weight_artifact = 0.7
"""


DEFAULTS_TOML = """\
[task]
id = "defaults-001"
repo = "org/repo"

[metadata]
name = "defaults"
"""


class TestDualInlineToml:
    def test_scoring_policy_min(self, tmp_path: Path) -> None:
        p = tmp_path / "task.toml"
        p.write_text(DUAL_MIN_TOML)
        task = load_task(p)

        assert task.verification.verification_mode == "dual"
        assert task.verification.scoring_policy == "min"
        # Weights fall back to the dataclass defaults when unspecified.
        assert task.verification.weight_direct == 0.5
        assert task.verification.weight_artifact == 0.5

    def test_scoring_policy_weighted_round_trip(self, tmp_path: Path) -> None:
        p = tmp_path / "task.toml"
        p.write_text(DUAL_WEIGHTED_TOML)
        task = load_task(p)

        assert task.verification.verification_mode == "dual"
        assert task.verification.scoring_policy == "weighted"
        assert task.verification.weight_direct == 0.3
        assert task.verification.weight_artifact == 0.7

    def test_defaults_when_verification_section_absent(self, tmp_path: Path) -> None:
        p = tmp_path / "task.toml"
        p.write_text(DEFAULTS_TOML)
        task = load_task(p)

        assert task.verification.verification_mode == "test_script"
        assert task.verification.scoring_policy == ""
        assert task.verification.weight_direct == 0.5
        assert task.verification.weight_artifact == 0.5
