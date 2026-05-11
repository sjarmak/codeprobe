"""Scaffold-mode coverage for ``quarantine_local_source`` (codeprobe-2nw2).

This module is gated on a sentinel constant
``codeprobe.core.isolation.SGONLY_SCAFFOLD_AVAILABLE`` which is set
True by codeprobe-yw6u (.2) when the ``mode="scaffold"`` parameter
lands on ``quarantine_local_source``. Until that bead shipped, the
entire module skipped at collection time.

**Scope rule (do not violate).** Tests in this file MUST instantiate
``quarantine_local_source`` with ``mode="scaffold"`` only. Hide-mode
coverage lives in ``tests/test_isolation.py::TestQuarantineLocalSource``
and MUST NOT be duplicated here. When adding new tests, audit
hide-mode coverage first; if a test would be equally valid for both
modes, parametrise the existing test rather than copy-pasting into
this file.

Test classes:

- **`TestScaffoldOverlayFilter`** — five overlay-filter cases captured
  from architect review H2: untouched placeholder, agent-written
  placeholder, ``.git/`` write rejection, ``.codeprobe/`` write
  rejection, agent ``answer.txt`` at workspace root.
- **`TestScaffoldManifest`** — three manifest invariants: location
  outside workspace, schema shape, deletion on exit.
- **`TestScaffoldSmokeFixture`** — two end-to-end round-trips against
  ``tests/fixtures/sdlc_sgonly_smoke``: zero-byte view during yield,
  oracle pass post-restore.

The codeprobe-sm9f bead owns verifier-side integration tests (executor
flow plus the merged-state oracle assertion) which live outside this
module. The 10 cases below cover the context-manager mechanism the
yw6u bead implements.

Reference: ``docs/investigations/codeprobe-2nw2/design.md``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from codeprobe.core import isolation
from codeprobe.core.isolation import quarantine_local_source

# Belt-and-suspenders skip gate. The module-level ``pytest.skip`` below
# stops collection if the sentinel is missing. If a future refactor
# removes that gate, ``pytestmark`` keeps every test individually
# skipped so the suite still stays green.
pytestmark = pytest.mark.skipif(
    not getattr(isolation, "SGONLY_SCAFFOLD_AVAILABLE", False),
    reason="scaffold mode not yet implemented — waits for codeprobe-yw6u",
)

if not getattr(isolation, "SGONLY_SCAFFOLD_AVAILABLE", False):
    pytest.skip(
        "scaffold mode not yet implemented — waits for codeprobe-yw6u",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Five overlay-filter cases (architect review H2)
# ---------------------------------------------------------------------------


class TestScaffoldOverlayFilter:
    """The five fixture cases the overlay filter MUST get right.

    Each test sets up a workspace with one of the five scenarios, runs
    ``quarantine_local_source(ws, mode="scaffold")``, has the simulated
    agent perform one specific action, and asserts the post-exit state.
    """

    def test_untouched_placeholder_restores_to_original_source(
        self, tmp_path: Path
    ) -> None:
        """Case 1: placeholder left at 0 bytes → real source wins on restore."""
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "src").mkdir()
        (ws / "src" / "main.py").write_text("ORIGINAL")

        with quarantine_local_source(ws, mode="scaffold"):
            placeholder = ws / "src" / "main.py"
            assert placeholder.exists()
            assert placeholder.stat().st_size == 0
            # Agent leaves the placeholder untouched.

        assert (ws / "src" / "main.py").read_text() == "ORIGINAL"

    def test_agent_written_placeholder_overlays_over_restored_source(
        self, tmp_path: Path
    ) -> None:
        """Case 2: agent grows placeholder 0 → N bytes → overlay wins."""
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "src").mkdir()
        (ws / "src" / "main.py").write_text("ORIGINAL")

        with quarantine_local_source(ws, mode="scaffold"):
            (ws / "src" / "main.py").write_text("AGENT_EDIT")

        assert (ws / "src" / "main.py").read_text() == "AGENT_EDIT"

    def test_dot_git_writes_are_not_overlaid(self, tmp_path: Path) -> None:
        """Case 3: agent touches .git/index → MUST NOT overlay (corrupts repo)."""
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".git").mkdir()
        (ws / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (ws / "src.py").write_text("ORIG")

        with quarantine_local_source(ws, mode="scaffold"):
            # .git is in the keep set; agent writes to it would be its own
            # business. The overlay step MUST NOT capture those writes —
            # they'd otherwise be re-applied on top of restored source.
            (ws / ".git" / "index").write_text("HOSTILE")

        # .git/HEAD still readable. The hostile write is not propagated
        # via overlay; it's just present because keep dirs are not
        # touched by the context manager at all.
        assert (ws / ".git" / "HEAD").read_text() == "ref: refs/heads/main\n"
        assert (ws / "src.py").read_text() == "ORIG"
        # The hostile file was written directly into .git (which is
        # kept), so it remains on disk — that's not the overlay's
        # business. What matters: the overlay didn't COPY it elsewhere.
        # We assert no stash leftover (overlay dir removed with stash).
        leftover = [p for p in tmp_path.iterdir() if p.name.startswith(".codeprobe-source-stash-")]
        assert leftover == []

    def test_dot_codeprobe_writes_are_not_overlaid(self, tmp_path: Path) -> None:
        """Case 4: agent touches .codeprobe/* → MUST NOT overlay (metadata)."""
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".codeprobe").mkdir()
        (ws / ".codeprobe" / "marker").write_text("M")
        (ws / "src.py").write_text("ORIG")

        with quarantine_local_source(ws, mode="scaffold"):
            (ws / ".codeprobe" / "hostile").write_text("AGENT_DATA")

        assert (ws / ".codeprobe" / "marker").read_text() == "M"
        assert (ws / "src.py").read_text() == "ORIG"
        leftover = [p for p in tmp_path.iterdir() if p.name.startswith(".codeprobe-source-stash-")]
        assert leftover == []

    def test_agent_answer_at_workspace_root_is_overlaid(
        self, tmp_path: Path
    ) -> None:
        """Case 5: agent writes answer.txt at workspace root → overlay wins."""
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "src.py").write_text("ORIG")

        with quarantine_local_source(ws, mode="scaffold"):
            (ws / "answer.txt").write_text("agent output")

        assert (ws / "src.py").read_text() == "ORIG"
        assert (ws / "answer.txt").read_text() == "agent output"


# ---------------------------------------------------------------------------
# Manifest invariants (architect review C1)
# ---------------------------------------------------------------------------


class TestScaffoldManifest:
    """Invariants on the on-disk manifest written by scaffold mode."""

    def test_manifest_is_outside_workspace(self, tmp_path: Path) -> None:
        """Manifest must live inside stash dir, not workspace."""
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "main.py").write_text("ORIG")

        observed_manifest_paths: list[Path] = []

        with quarantine_local_source(ws, mode="scaffold"):
            # Inside the yield, the manifest must NOT be visible inside
            # the workspace — otherwise it'd be stashed too.
            assert not (ws / "manifest.json").exists()
            assert not (ws / ".codeprobe-sgonly-manifest.json").exists()
            # Find the stash dir; manifest lives inside it.
            for sibling in tmp_path.iterdir():
                if sibling.name.startswith(".codeprobe-source-stash-"):
                    manifest = sibling / "manifest.json"
                    if manifest.exists():
                        observed_manifest_paths.append(manifest)

        assert len(observed_manifest_paths) == 1, (
            f"expected exactly one manifest in a sibling stash, "
            f"got {observed_manifest_paths}"
        )
        manifest_path = observed_manifest_paths[0]
        # Confirm it lives under <workspace>.parent, not inside workspace.
        assert manifest_path.parent.parent == ws.parent

    def test_manifest_schema_matches_design(self, tmp_path: Path) -> None:
        """Manifest must carry: mode, stash_dir, scaffold_paths, created_at."""
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "src").mkdir()
        (ws / "src" / "a.py").write_text("a")
        (ws / "src" / "b.go").write_text("b")
        (ws / "data.yaml").write_text("k: v")

        manifest_snapshot: dict[str, object] = {}

        with quarantine_local_source(ws, mode="scaffold"):
            stash_dirs = [
                p for p in tmp_path.iterdir()
                if p.name.startswith(".codeprobe-source-stash-")
            ]
            assert len(stash_dirs) == 1
            manifest = stash_dirs[0] / "manifest.json"
            manifest_snapshot.update(json.loads(manifest.read_text()))

        assert manifest_snapshot["mode"] == "scaffold"
        assert manifest_snapshot["stash_dir"]  # non-empty absolute path
        assert Path(str(manifest_snapshot["stash_dir"])).is_absolute()
        paths = manifest_snapshot["scaffold_paths"]
        assert isinstance(paths, list)
        assert set(paths) == {"src/a.py", "src/b.go", "data.yaml"}
        # ISO-8601 UTC timestamp ending in Z (per design doc).
        created_at = manifest_snapshot["created_at"]
        assert isinstance(created_at, str)
        assert created_at.endswith("Z")

    def test_manifest_deleted_after_exit(self, tmp_path: Path) -> None:
        """Manifest dies with the stash dir on context exit."""
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "main.py").write_text("ORIG")

        with quarantine_local_source(ws, mode="scaffold"):
            pass

        # No leftover stash, hence no leftover manifest.
        leftover = [
            p for p in tmp_path.iterdir()
            if p.name.startswith(".codeprobe-source-stash-")
        ]
        assert leftover == []


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

    def _seed_workspace(self, smoke_fixture_root: Path, tmp_path: Path) -> Path:
        """Copy the read-only fixture into a writable workspace dir."""
        import shutil as _shutil

        ws = tmp_path / "ws"
        _shutil.copytree(smoke_fixture_root, ws)
        return ws

    def test_yield_window_sees_zero_byte_placeholders(
        self, smoke_fixture_root: Path, tmp_path: Path
    ) -> None:
        """Inside the yield, src/math.go is 0 bytes, not the original content."""
        ws = self._seed_workspace(smoke_fixture_root, tmp_path)
        assert (ws / "src" / "math.go").stat().st_size > 0

        with quarantine_local_source(ws, mode="scaffold"):
            placeholder = ws / "src" / "math.go"
            assert placeholder.exists()
            assert placeholder.stat().st_size == 0
            # tests/ is stashed (not in keep set), so its content is
            # hidden from the agent during the yield.
            assert not (ws / "tests").exists()

    def test_post_exit_state_passes_oracle(
        self, smoke_fixture_root: Path, tmp_path: Path
    ) -> None:
        """Simulated agent adds func add → tests/test.sh exits 0 post-restore."""
        ws = self._seed_workspace(smoke_fixture_root, tmp_path)

        agent_program = (
            "package math\n\n"
            "// existing\n\n"
            "func add(a int, b int) int {\n"
            "    return a + b\n"
            "}\n"
        )

        with quarantine_local_source(ws, mode="scaffold"):
            (ws / "src" / "math.go").write_text(agent_program)

        # Post-exit: source restored, overlay applied.
        merged = (ws / "src" / "math.go").read_text()
        assert "// existing" in merged
        assert "func add" in merged
        # The oracle (bash tests/test.sh) sees this merged state.
        result = subprocess.run(
            ["bash", str(ws / "tests" / "test.sh")],
            env={"TASK_REPO_ROOT": str(ws), "PATH": "/usr/bin:/bin"},
            cwd=ws,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"oracle failed unexpectedly: stdout={result.stdout!r} "
            f"stderr={result.stderr!r}"
        )
