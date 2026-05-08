"""Tests for the null/golden/adversarial calibration triad."""

from __future__ import annotations

import json
import stat
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.calibration.triad import (
    BAND_LIMITS,
    FAMILIES,
    discover_calibration_tasks,
    is_synthetic_task,
    run_triad,
    synthesize_adversarial_output,
    synthesize_golden_output,
    synthesize_null_output,
)
from codeprobe.cli import main

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def binary_count_task(tmp_path: Path) -> Path:
    """A binary-scored count task: agent must output the integer 7."""
    task = tmp_path / "count-things"
    (task / "tests").mkdir(parents=True)
    (task / "task.toml").write_text(
        textwrap.dedent(
            """
            [task]
            id = "count-things"
            repo = "synthetic"
            time_limit_sec = 30

            [metadata]
            name = "count-things"
            difficulty = "easy"
            description = "Count things"
            language = "python"
            task_type = "architecture_comprehension"

            [verification]
            type = "test_script"
            command = "bash tests/test.sh"
            reward_type = "binary"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    (task / "instruction.md").write_text("# Count\nReturn 7.\n", encoding="utf-8")
    (task / "tests" / "ground_truth.json").write_text(
        json.dumps({"answer_type": "integer", "answer": 7}),
        encoding="utf-8",
    )
    test_sh = task / "tests" / "test.sh"
    test_sh.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env bash
            set -euo pipefail
            if [ -n "${AGENT_OUTPUT:-}" ] && [ -f "$AGENT_OUTPUT" ]; then
                ACTUAL=$(cat "$AGENT_OUTPUT")
            elif [ -f answer.txt ]; then
                ACTUAL=$(cat answer.txt)
            else
                ACTUAL=""
            fi
            ACTUAL=$(echo "$ACTUAL" | grep -oE '[0-9]+' | head -1 || echo "")
            EXPECTED=7
            test "$ACTUAL" = "$EXPECTED"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    _make_executable(test_sh)
    return task


@pytest.fixture
def continuous_file_list_task(tmp_path: Path) -> Path:
    """A continuous-scored file_list task with an oracle.py F1 scorer."""
    task = tmp_path / "file-discovery"
    (task / "tests").mkdir(parents=True)
    (task / "metadata.json").write_text(
        json.dumps(
            {
                "id": "file-discovery",
                "repo": "synthetic",
                "metadata": {
                    "name": "file-discovery",
                    "difficulty": "medium",
                    "description": "Find files affected by symbol X",
                    "language": "python",
                    "task_type": "sdlc_code_change",
                },
                "verification": {
                    "type": "oracle",
                    "command": "bash tests/test.sh",
                    "reward_type": "continuous",
                    "oracle_type": "file_list",
                },
            }
        ),
        encoding="utf-8",
    )
    (task / "instruction.md").write_text(
        "# Find files\nReturn the list of files.\n", encoding="utf-8"
    )
    (task / "tests" / "ground_truth.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "oracle_type": "file_list",
                "expected": [
                    "src/foo.py",
                    "src/bar.py",
                    "src/baz.py",
                    "tests/test_foo.py",
                    "tests/test_bar.py",
                ],
            }
        ),
        encoding="utf-8",
    )
    oracle_py = task / "tests" / "oracle.py"
    oracle_py.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env python3
            import json, sys
            from pathlib import Path

            def normalize(p):
                p = p.replace("\\\\", "/").strip()
                while p.startswith("./"):
                    p = p[2:]
                return p.lstrip("/")

            def main():
                task_dir = Path(sys.argv[1])
                gt = json.loads((task_dir / "tests" / "ground_truth.json").read_text())
                expected = frozenset(normalize(p) for p in gt.get("expected", []))
                ans_path = task_dir / "answer.txt"
                if not ans_path.exists():
                    (task_dir / "reward.txt").write_text("0.0\\n")
                    print("score=0.0")
                    sys.exit(0)
                lines = ans_path.read_text().splitlines()
                agent = frozenset(
                    normalize(l) for l in lines if l.strip() and not l.startswith("#")
                )
                if not expected or not agent:
                    (task_dir / "reward.txt").write_text("0.0\\n")
                    print("score=0.0")
                    sys.exit(0)
                inter = len(expected & agent)
                p = inter / len(agent)
                r = inter / len(expected)
                f1 = 2*p*r/(p+r) if p+r else 0.0
                (task_dir / "reward.txt").write_text(f"{f1:.4f}\\n")
                print(f"score={f1:.4f} precision={p:.4f} recall={r:.4f}")

            main()
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    _make_executable(oracle_py)
    test_sh = task / "tests" / "test.sh"
    test_sh.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env bash
            set -euo pipefail
            SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
            TASK_DIR="$(dirname "$SCRIPT_DIR")"
            if [ ! -f "$TASK_DIR/answer.txt" ] && [ -n "${AGENT_OUTPUT:-}" ] \\
                && [ -f "$AGENT_OUTPUT" ]; then
                cp "$AGENT_OUTPUT" "$TASK_DIR/answer.txt"
            fi
            python3 "$SCRIPT_DIR/oracle.py" "$TASK_DIR"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    _make_executable(test_sh)
    return task


@pytest.fixture
def stale_answer_task(tmp_path: Path, continuous_file_list_task: Path) -> Path:
    """File-list task that ships a *stale* answer.txt unrelated to the oracle.

    Mirrors the real-world failure mode where a manual edit left a stale
    answer.txt against an updated ground_truth.json. The triad must score
    the synthesised golden output, not the stale answer.
    """
    (continuous_file_list_task / "answer.txt").write_text(
        "src/STALE_unrelated.py\n", encoding="utf-8"
    )
    return continuous_file_list_task


# ---------------------------------------------------------------------------
# Fixture synthesis
# ---------------------------------------------------------------------------


def test_null_output_is_empty(tmp_path: Path) -> None:
    assert synthesize_null_output(tmp_path) == ""


def test_golden_from_v1_scalar(binary_count_task: Path) -> None:
    assert synthesize_golden_output(binary_count_task) == "7"


def test_golden_from_v1_list(continuous_file_list_task: Path) -> None:
    out = synthesize_golden_output(continuous_file_list_task)
    assert out.splitlines() == [
        "src/foo.py",
        "src/bar.py",
        "src/baz.py",
        "tests/test_foo.py",
        "tests/test_bar.py",
    ]


def test_golden_prefers_ground_truth_over_stale_answer_txt(
    stale_answer_task: Path,
) -> None:
    out = synthesize_golden_output(stale_answer_task)
    assert "STALE_unrelated" not in out
    assert "src/foo.py" in out


def test_golden_falls_back_to_answer_txt_when_no_ground_truth(
    tmp_path: Path,
) -> None:
    task = tmp_path / "behavioural"
    (task / "tests").mkdir(parents=True)
    (task / "task.toml").write_text(
        '[task]\nid = "behavioural"\nrepo = "x"\n', encoding="utf-8"
    )
    (task / "answer.txt").write_text("hello world\n", encoding="utf-8")
    assert synthesize_golden_output(task).strip() == "hello world"


def test_adversarial_includes_oracle_tokens_plus_distractors(
    continuous_file_list_task: Path,
) -> None:
    out = synthesize_adversarial_output(continuous_file_list_task)
    lines = out.splitlines()
    assert "src/foo.py" in lines
    assert "tests/test_bar.py" in lines
    assert any("_unrelated/" in line for line in lines)
    # Distractors should outnumber oracle tokens by enough that precision
    # falls under 0.5 — the band the calibration triad enforces.
    distractor_count = sum(1 for line in lines if "_unrelated/" in line)
    assert distractor_count >= 60


# ---------------------------------------------------------------------------
# Triad runner
# ---------------------------------------------------------------------------


def test_triad_passes_for_well_calibrated_binary_task(
    binary_count_task: Path,
) -> None:
    result = run_triad(binary_count_task)
    assert result.error is None
    assert {f.family for f in result.fixtures} == set(FAMILIES)
    by_fam = result.fixtures_by_family
    assert by_fam["null"].band_pass, by_fam["null"].reward
    assert by_fam["golden"].band_pass, by_fam["golden"].reward
    assert by_fam["adversarial"].band_pass, by_fam["adversarial"].reward
    assert result.all_pass


def test_triad_passes_for_well_calibrated_continuous_task(
    continuous_file_list_task: Path,
) -> None:
    result = run_triad(continuous_file_list_task)
    assert result.error is None
    by_fam = result.fixtures_by_family
    assert by_fam["null"].reward == 0.0
    assert by_fam["golden"].reward >= 0.9
    # Adversarial: 5 oracle files + 80 distractors → precision = 5/85 =
    # 0.059, recall = 1.0, F1 = 2*0.059*1/1.059 ≈ 0.111 → well under 0.5.
    assert by_fam["adversarial"].reward < 0.5
    assert result.all_pass


def test_triad_band_pass_uses_unified_reward_field(
    continuous_file_list_task: Path,
) -> None:
    result = run_triad(continuous_file_list_task)
    for fx in result.fixtures:
        # `reward` is the canonical field per the unified ScoreResult
        # contract; band_pass must read it (not legacy `score`).
        lo, hi = BAND_LIMITS[fx.family]
        assert fx.band_pass == (lo <= fx.reward <= hi)


def test_triad_surfaces_band_breach_when_test_sh_is_a_no_op(
    tmp_path: Path,
) -> None:
    """A test.sh that ignores agent output should fail null+adversarial bands."""
    task = tmp_path / "broken-rubric"
    (task / "tests").mkdir(parents=True)
    (task / "task.toml").write_text(
        '[task]\nid = "broken-rubric"\nrepo = "x"\n[verification]\nreward_type = "binary"\n',
        encoding="utf-8",
    )
    (task / "instruction.md").write_text("noop", encoding="utf-8")
    (task / "tests" / "ground_truth.json").write_text(
        json.dumps({"answer_type": "text", "answer": "ok"}), encoding="utf-8"
    )
    test_sh = task / "tests" / "test.sh"
    test_sh.write_text(
        "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8"
    )
    _make_executable(test_sh)

    result = run_triad(task)
    by_fam = result.fixtures_by_family
    assert by_fam["null"].reward == 1.0  # exit 0 → reward 1.0
    assert not by_fam["null"].band_pass
    assert by_fam["golden"].band_pass
    assert not by_fam["adversarial"].band_pass
    assert not result.all_pass


def test_triad_to_dict_round_trip(binary_count_task: Path) -> None:
    result = run_triad(binary_count_task)
    payload = result.to_dict()
    serialised = json.dumps(payload, sort_keys=True)
    parsed = json.loads(serialised)
    assert parsed["task_id"] == "count-things"
    assert parsed["all_pass"] is True
    fams = [f["family"] for f in parsed["fixtures"]]
    assert fams == list(FAMILIES)
    for fx in parsed["fixtures"]:
        assert "reward" in fx
        assert "band" in fx
        assert "band_pass" in fx
        assert "scorer_family" in fx


def test_triad_handles_missing_task_dir(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    result = run_triad(missing)
    assert result.error is not None
    assert "does not exist" in result.error
    assert result.fixtures == ()
    assert not result.all_pass


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discover_calibration_tasks_finds_test_sh_dirs(
    tmp_path: Path,
    binary_count_task: Path,
    continuous_file_list_task: Path,
) -> None:
    found = discover_calibration_tasks([tmp_path])
    names = {p.name for p in found}
    assert names == {"count-things", "file-discovery"}


def test_discover_skips_non_task_dirs(tmp_path: Path) -> None:
    (tmp_path / "random").mkdir()
    (tmp_path / "random" / "README.md").write_text("not a task")
    assert discover_calibration_tasks([tmp_path]) == []


def _make_synthetic_task(parent: Path, name: str) -> Path:
    """Create a minimal task dir flagged ``synthetic = true``."""
    task = parent / name
    (task / "tests").mkdir(parents=True)
    (task / "task.toml").write_text(
        textwrap.dedent(
            f"""
            [task]
            id = "{name}"
            repo = "synthetic"

            [metadata]
            name = "{name}"
            synthetic = true

            [verification]
            type = "test_script"
            command = "bash tests/test.sh"
            reward_type = "binary"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    (task / "instruction.md").write_text("noop", encoding="utf-8")
    test_sh = task / "tests" / "test.sh"
    test_sh.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    _make_executable(test_sh)
    (task / "tests" / "ground_truth.json").write_text(
        json.dumps({"answer_type": "text", "answer": "ok"}), encoding="utf-8"
    )
    return task


def test_discover_skips_synthetic_tasks_by_default(
    tmp_path: Path,
    binary_count_task: Path,
) -> None:
    """Tasks with ``synthetic = true`` must be omitted from default discovery."""
    _make_synthetic_task(tmp_path, "synthetic-noop")
    found = discover_calibration_tasks([tmp_path])
    names = {p.name for p in found}
    assert names == {"count-things"}, found


def test_discover_includes_synthetic_when_flag_set(
    tmp_path: Path,
    binary_count_task: Path,
) -> None:
    _make_synthetic_task(tmp_path, "synthetic-noop")
    found = discover_calibration_tasks([tmp_path], include_synthetic=True)
    names = {p.name for p in found}
    assert names == {"count-things", "synthetic-noop"}, found


def test_is_synthetic_task_reads_metadata_flag(
    tmp_path: Path,
    binary_count_task: Path,
) -> None:
    synth = _make_synthetic_task(tmp_path, "synthetic-noop")
    assert is_synthetic_task(synth) is True
    assert is_synthetic_task(binary_count_task) is False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_calibrate_triad_writes_per_task_json_and_report(
    tmp_path: Path,
    binary_count_task: Path,
) -> None:
    runner = CliRunner()
    out_dir = tmp_path / "calib-out"
    report = tmp_path / "report.md"
    result = runner.invoke(
        main,
        [
            "calibrate-triad",
            str(binary_count_task),
            "--out-dir",
            str(out_dir),
            "--report",
            str(report),
            "--no-json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (out_dir / "count-things.json").is_file()
    assert report.is_file()
    payload = json.loads((out_dir / "count-things.json").read_text())
    assert payload["all_pass"] is True
    body = report.read_text(encoding="utf-8")
    assert "calibration triad" in body.lower()
    assert "count-things" in body


def test_cli_calibrate_triad_strict_exits_nonzero_on_breach(
    tmp_path: Path,
) -> None:
    """A no-op test.sh should make the strict mode return exit code 1."""
    task = tmp_path / "broken"
    (task / "tests").mkdir(parents=True)
    (task / "task.toml").write_text(
        '[task]\nid = "broken"\nrepo = "x"\n[verification]\nreward_type = "binary"\n',
        encoding="utf-8",
    )
    (task / "instruction.md").write_text("noop", encoding="utf-8")
    (task / "tests" / "ground_truth.json").write_text(
        json.dumps({"answer_type": "text", "answer": "ok"}), encoding="utf-8"
    )
    test_sh = task / "tests" / "test.sh"
    test_sh.write_text(
        "#!/usr/bin/env bash\nexit 0\n", encoding="utf-8"
    )
    _make_executable(test_sh)

    runner = CliRunner()
    out_dir = tmp_path / "calib-out"
    report = tmp_path / "report.md"
    result = runner.invoke(
        main,
        [
            "calibrate-triad",
            str(task),
            "--out-dir",
            str(out_dir),
            "--report",
            str(report),
            "--no-json",
        ],
    )
    assert result.exit_code == 1, result.output
    assert (out_dir / "broken.json").is_file()


def test_cli_calibrate_triad_skips_synthetic_when_walking_parent(
    tmp_path: Path,
    binary_count_task: Path,
) -> None:
    """When invoked on a parent dir, synthetic-tagged tasks are filtered out
    by default but pulled back in by ``--include-synthetic``."""
    _make_synthetic_task(tmp_path, "synthetic-noop")

    runner = CliRunner()
    out_dir = tmp_path / "calib-out"
    report = tmp_path / "report.md"

    result = runner.invoke(
        main,
        [
            "calibrate-triad",
            str(tmp_path),
            "--out-dir",
            str(out_dir),
            "--report",
            str(report),
            "--no-json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (out_dir / "count-things.json").is_file()
    assert not (out_dir / "synthetic-noop.json").exists()

    out_dir2 = tmp_path / "calib-out-incl"
    report2 = tmp_path / "report-incl.md"
    result2 = runner.invoke(
        main,
        [
            "calibrate-triad",
            str(tmp_path),
            "--out-dir",
            str(out_dir2),
            "--report",
            str(report2),
            "--include-synthetic",
            "--no-strict",
            "--no-json",
        ],
    )
    assert result2.exit_code == 0, result2.output
    assert (out_dir2 / "count-things.json").is_file()
    assert (out_dir2 / "synthetic-noop.json").is_file()
