"""AC3: tar → move → untar across a filesystem prefix still verifies.

The snapshot must be entirely self-describing: relative symlinks, relative
paths in the manifest, and no absolute references that break on relocation.
"""

from __future__ import annotations

import os
import tarfile
from pathlib import Path

from codeprobe.snapshot import create_snapshot, verify_snapshot_extended


def _make_experiment(tmp_path: Path) -> Path:
    exp = tmp_path / "experiment"
    for config in ("baseline", "mcp"):
        for task_id in ("task_0001", "task_0002"):
            trial = exp / config / task_id
            trial.mkdir(parents=True)
            (trial / "result.json").write_text(
                f'{{"config": "{config}", "task": "{task_id}"}}\n'
            )
            (trial / "task_metrics.json").write_text('{"reward": 0.5}\n')
            agent_dir = trial / "agent"
            agent_dir.mkdir()
            (agent_dir / "instruction.txt").write_text(f"do task {task_id}\n")
            verifier_dir = trial / "verifier"
            verifier_dir.mkdir()
            (verifier_dir / "reward.txt").write_text("1.0\n")
    return exp


def test_snapshot_relocation_preserves_verification(tmp_path: Path) -> None:
    """Tar the snapshot, move the archive, untar at a different prefix, verify."""
    exp = _make_experiment(tmp_path)
    snap_a = tmp_path / "a" / "snap"
    snap_a.parent.mkdir(parents=True)

    os.environ.pop("CODEPROBE_SIGNING_KEY", None)
    create_snapshot(exp, snap_a)

    # Sanity: pre-move verification is green.
    pre = verify_snapshot_extended(snap_a)
    assert pre.symlinks_contained is True
    assert pre.file_hashes_match is True

    # Tar the snapshot preserving relative paths.
    archive_path = tmp_path / "snap.tar"
    with tarfile.open(archive_path, "w") as tar:
        tar.add(snap_a, arcname="snap")

    # Move the archive to a different filesystem prefix and untar.
    dest_root = tmp_path / "b" / "deeper" / "prefix"
    dest_root.mkdir(parents=True)
    moved_archive = dest_root / "snap.tar"
    archive_path.rename(moved_archive)
    with tarfile.open(moved_archive, "r") as tar:
        tar.extractall(dest_root)

    snap_b = dest_root / "snap"
    assert (snap_b / "SNAPSHOT.json").exists()

    post = verify_snapshot_extended(snap_b)
    assert post.symlinks_contained is True, post.offending_paths
    assert post.file_hashes_match is True, post.offending_paths
    assert post.ok is True, post.reason


def test_snapshot_layout_has_csb_dirs(tmp_path: Path) -> None:
    """The CSB layout dirs must all exist after create_snapshot."""
    exp = _make_experiment(tmp_path)
    out = tmp_path / "snap"
    create_snapshot(exp, out)

    assert (out / "SNAPSHOT.json").exists()
    assert (out / "summary").is_dir()
    for name in ("rewards.json", "aggregate.json", "timing.json", "costs.json"):
        assert (out / "summary" / name).is_file(), name
    assert (out / "traces").is_dir()
    assert (out / "export" / "traces").is_dir()

    # Per-config/per-task layout exists under export/traces.
    assert (out / "export" / "traces" / "baseline" / "task_0001").is_dir()
    assert (out / "export" / "traces" / "mcp" / "task_0002").is_dir()


def test_snapshot_json_records_dependencies(tmp_path: Path) -> None:
    """SNAPSHOT.json.dependencies records the R18 dependency surface."""
    import json

    exp = _make_experiment(tmp_path)
    out = tmp_path / "snap"
    create_snapshot(exp, out)

    manifest = json.loads((out / "SNAPSHOT.json").read_text())
    assert manifest["schema_version"] == "1.0"
    assert "created_at" in manifest
    deps = manifest["dependencies"]
    assert "mcp_tools" in deps
    assert "llm_backends" in deps
    assert "issue_trackers" in deps
    assert "build_manifest_parsers" in deps

    # llm_backends should include the logical names from the r13 registry.
    logical_names = {entry["logical_name"] for entry in deps["llm_backends"]}
    assert "opus-4.7" in logical_names
    assert "sonnet-4.6" in logical_names
    assert "haiku-4.5" in logical_names

    # issue_trackers should include jira/github/gitlab with versions.
    tracker_names = {entry["name"] for entry in deps["issue_trackers"]}
    assert tracker_names == {"jira", "github", "gitlab"}
    for entry in deps["issue_trackers"]:
        assert entry["api_version"].startswith("v")

    # build_manifest_parsers includes codeprobe.
    parser_names = {entry["name"] for entry in deps["build_manifest_parsers"]}
    assert "codeprobe" in parser_names
