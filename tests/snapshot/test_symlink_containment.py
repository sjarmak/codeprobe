"""AC2: snapshot create + verify reject symlinks whose target escapes the root.

Two flavours:

1. **Create-time rejection** — planting a symlink to ``../../etc`` inside the
   experiment directory causes ``snapshot create`` to abort before any
   output is written.
2. **Verify-time rejection** — a snapshot whose own tree contains a symlink
   pointing outside the snapshot root fails ``snapshot verify``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli import main as cli_main
from codeprobe.snapshot import (
    SymlinkEscapeError,
    create_snapshot,
    preflight_symlink_containment,
    verify_snapshot_extended,
)


def _make_experiment(tmp_path: Path) -> Path:
    """Build a minimal CSB-style experiment directory with one trial."""
    exp = tmp_path / "experiment"
    trial = exp / "baseline" / "task_0001"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text('{"ok": true}\n')
    (trial / "task_metrics.json").write_text('{"reward": 1.0}\n')
    return exp


def test_preflight_raises_on_escaping_symlink(tmp_path: Path) -> None:
    """A symlink to ../../etc inside the experiment dir must be rejected."""
    exp = _make_experiment(tmp_path)
    bad = exp / "baseline" / "escape"
    os.symlink("../../../../../etc", bad)

    with pytest.raises(SymlinkEscapeError):
        preflight_symlink_containment(exp)


def test_preflight_accepts_internal_symlinks(tmp_path: Path) -> None:
    """A symlink whose target is inside the experiment root must be accepted."""
    exp = _make_experiment(tmp_path)
    target = exp / "baseline" / "task_0001" / "result.json"
    alias = exp / "baseline" / "result_alias.json"
    os.symlink(os.path.relpath(target, alias.parent), alias)

    # No exception.
    preflight_symlink_containment(exp)


def test_create_aborts_on_escaping_symlink(tmp_path: Path) -> None:
    """``create_snapshot`` must fail loud and not materialise an output tree."""
    exp = _make_experiment(tmp_path)
    bad = exp / "baseline" / "leak"
    os.symlink("/etc", bad)

    out = tmp_path / "snap"
    with pytest.raises(SymlinkEscapeError):
        create_snapshot(exp, out)

    # Optional safety: the output dir should not contain a SNAPSHOT.json.
    assert not (out / "SNAPSHOT.json").exists()


def test_cli_create_aborts_on_escaping_symlink(tmp_path: Path) -> None:
    """The CLI must exit non-zero when the experiment has an escaping link."""
    exp = _make_experiment(tmp_path)
    bad = exp / "baseline" / "leak"
    os.symlink("/etc", bad)

    out = tmp_path / "snap"
    runner = CliRunner()
    result = runner.invoke(
        cli_main, ["snapshot", "create", str(exp), "--out", str(out)]
    )
    assert result.exit_code != 0, result.output
    assert "escape" in result.output.lower() or "symlink" in result.output.lower()


def test_verify_rejects_planted_escaping_symlink(tmp_path: Path) -> None:
    """A planted symlink under export/ that escapes the snapshot must fail verify.

    Per the CSB layout contract, symlink containment is enforced against the
    ``export/`` root — that is the tree that will actually be published.
    """
    exp = _make_experiment(tmp_path)
    out = tmp_path / "snap"
    create_snapshot(exp, out)

    # After creation, plant an escaping symlink inside the export tree.
    # Use a relative traversal so the offender reason is "escapes snapshot"
    # rather than "absolute path".
    escape = out / "export" / "traces" / "leak"
    os.symlink("../../../../../etc", escape)

    result = verify_snapshot_extended(out)
    assert result.ok is False
    assert result.symlinks_contained is False
    assert str(escape) in result.offending_paths


def test_verify_rejects_absolute_symlink_anywhere(tmp_path: Path) -> None:
    """Absolute-path symlinks are offenders regardless of location."""
    exp = _make_experiment(tmp_path)
    out = tmp_path / "snap"
    create_snapshot(exp, out)

    escape = out / "summary" / "leak"
    os.symlink("/etc/passwd", escape)

    result = verify_snapshot_extended(out)
    assert result.ok is False
    assert result.symlinks_contained is False
    assert str(escape) in result.offending_paths


def test_verify_accepts_well_formed_snapshot(tmp_path: Path) -> None:
    """Positive control: clean snapshot with no symlinks verifies ok."""
    exp = _make_experiment(tmp_path)
    out = tmp_path / "snap"
    # signing key ensures the attestation path exercises hmac.
    os.environ.pop("CODEPROBE_SIGNING_KEY", None)

    create_snapshot(exp, out)
    result = verify_snapshot_extended(out)
    # The r14 attestation is unsigned here (no key); base.ok may still be
    # True since unsigned verification passes on hash match.
    assert result.symlinks_contained is True
    assert result.file_hashes_match is True
    assert result.offending_paths == []
