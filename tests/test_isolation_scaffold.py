"""Scaffold-mode coverage for ``quarantine_local_source`` (codeprobe-2nw2).

This module is gated on a sentinel constant
``codeprobe.core.isolation.SGONLY_SCAFFOLD_AVAILABLE`` which is added
by codeprobe-yw6u (.2) when the ``mode="scaffold"`` parameter lands on
``quarantine_local_source``. Until that bead ships, the entire module
skips at collection time so ``pytest`` stays green.

**Scope rule (do not violate).** Tests in this file MUST instantiate
``quarantine_local_source`` with ``mode="scaffold"`` only. Hide-mode
coverage lives in ``tests/test_isolation.py::TestQuarantineLocalSource``
and MUST NOT be duplicated here. When adding new tests, audit
hide-mode coverage first; if a test would be equally valid for both
modes, parametrise the existing test rather than copy-pasting into
this file.

The stub surface codeprobe-sm9f (.3) must fill in:

- **`TestScaffoldOverlayFilter`** — five overlay-filter cases captured
  from architect review H2: untouched placeholder, agent-written
  placeholder, ``.git/`` write rejection, ``.codeprobe/`` write
  rejection, agent ``answer.txt`` at workspace root.
- **`TestScaffoldManifest`** — three manifest invariants: location
  outside workspace, schema shape, deletion on exit.
- **`TestScaffoldSmokeFixture`** — two end-to-end round-trips against
  ``tests/fixtures/sdlc_sgonly_smoke``: zero-byte view during yield,
  oracle pass post-restore.

Ten stubs total. All call ``pytest.skip`` until sm9f fills them in.

Reference: ``docs/investigations/codeprobe-2nw2/design.md``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeprobe.core import isolation

# Belt-and-suspenders skip gate. The module-level ``pytest.skip`` below
# stops collection if the sentinel is missing. If a future refactor
# removes that gate, ``pytestmark`` keeps every test individually
# skipped so the suite still stays green.
pytestmark = pytest.mark.skipif(
    not getattr(isolation, "SGONLY_SCAFFOLD_AVAILABLE", False),
    reason="scaffold mode not yet implemented — waits for codeprobe-yw6u",
)

# Gate the entire module on the codeprobe-yw6u sentinel. When that bead
# lands and exports ``SGONLY_SCAFFOLD_AVAILABLE = True`` at module
# scope of ``codeprobe.core.isolation``, every test below becomes live.
# Until then the file is collectable (no ImportError) but every test
# skips, so CI stays green.
if not getattr(isolation, "SGONLY_SCAFFOLD_AVAILABLE", False):
    pytest.skip(
        "scaffold mode not yet implemented — waits for codeprobe-yw6u",
        allow_module_level=True,
    )


_PENDING = "filled in by codeprobe-sm9f"


# ---------------------------------------------------------------------------
# Five overlay-filter cases (architect review H2)
# ---------------------------------------------------------------------------


class TestScaffoldOverlayFilter:
    """The five fixture cases the overlay filter MUST get right.

    Each test sets up a workspace with one of the five scenarios, runs
    ``quarantine_local_source(ws, mode="scaffold")``, has the simulated
    agent perform one specific action, and asserts the post-exit state.

    These tests describe behaviour codeprobe-yw6u must implement and
    codeprobe-sm9f must wire into the verifier.
    """

    def test_untouched_placeholder_restores_to_original_source(
        self, tmp_path: Path
    ) -> None:
        """Case 1: placeholder left at 0 bytes → real source wins on restore."""
        pytest.skip(_PENDING)

    def test_agent_written_placeholder_overlays_over_restored_source(
        self, tmp_path: Path
    ) -> None:
        """Case 2: agent grows placeholder 0 → N bytes → overlay wins."""
        pytest.skip(_PENDING)

    def test_dot_git_writes_are_not_overlaid(self, tmp_path: Path) -> None:
        """Case 3: agent touches .git/index → MUST NOT overlay (corrupts repo)."""
        pytest.skip(_PENDING)

    def test_dot_codeprobe_writes_are_not_overlaid(self, tmp_path: Path) -> None:
        """Case 4: agent touches .codeprobe/* → MUST NOT overlay (metadata)."""
        pytest.skip(_PENDING)

    def test_agent_answer_at_workspace_root_is_overlaid(
        self, tmp_path: Path
    ) -> None:
        """Case 5: agent writes answer.txt at workspace root → overlay wins."""
        pytest.skip(_PENDING)


# ---------------------------------------------------------------------------
# Manifest invariants (architect review C1)
# ---------------------------------------------------------------------------


class TestScaffoldManifest:
    """Invariants on the on-disk manifest written by scaffold mode."""

    def test_manifest_is_outside_workspace(self, tmp_path: Path) -> None:
        """Manifest must live inside stash dir, not workspace.

        Putting it inside the workspace would cause the manifest itself
        to be quarantined during the yield window — and the verifier
        needs to read it during ``__exit__``.
        """
        pytest.skip(_PENDING)

    def test_manifest_schema_matches_design(self, tmp_path: Path) -> None:
        """Manifest must carry: mode, stash_dir, scaffold_paths, created_at."""
        pytest.skip(_PENDING)

    def test_manifest_deleted_after_exit(self, tmp_path: Path) -> None:
        """Manifest dies with the stash dir on context exit."""
        pytest.skip(_PENDING)


# ---------------------------------------------------------------------------
# Smoke fixture round-trip (architect review C2)
# ---------------------------------------------------------------------------


class TestScaffoldSmokeFixture:
    """End-to-end against ``tests/fixtures/sdlc_sgonly_smoke``.

    This is the integration target codeprobe-hcnv (.4) wires into the
    full executor → smoke-trial path. Here we just validate the
    context manager round-trips the fixture correctly.
    """

    @pytest.fixture
    def smoke_fixture_root(self) -> Path:
        return Path(__file__).parent / "fixtures" / "sdlc_sgonly_smoke"

    def test_yield_window_sees_zero_byte_placeholders(
        self, smoke_fixture_root: Path, tmp_path: Path
    ) -> None:
        """Inside the yield, src/math.go is 0 bytes, not the original content."""
        pytest.skip(_PENDING)

    def test_post_exit_state_passes_oracle(
        self, smoke_fixture_root: Path, tmp_path: Path
    ) -> None:
        """Simulated agent adds func add → tests/test.sh exits 0 post-restore."""
        pytest.skip(_PENDING)
