"""Tests for templates/answer_json_verifier_lib.sh — CSB compatibility shim.

Verifies that the shell wrapper sources cleanly on bash 3.2-compatible syntax,
delegates to ``python3 -m codeprobe.core.scoring --artifact``, writes
reward.txt, and passes through the Python exit code.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
SHELL_LIB: Path = REPO_ROOT / "templates" / "answer_json_verifier_lib.sh"


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _run_bash(script: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a bash -c script with PYTHONPATH pointing at src/ for the module."""
    env = os.environ.copy()
    src_path = str(REPO_ROOT / "src")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{src_path}:{existing}" if existing else src_path
    return subprocess.run(
        ["bash", "-c", script],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_shell_lib_exists() -> None:
    assert SHELL_LIB.is_file(), f"Missing shell lib at {SHELL_LIB}"


def test_shell_lib_has_shebang_comment() -> None:
    content = SHELL_LIB.read_text(encoding="utf-8")
    assert content.startswith("#!/bin/bash"), "Shell lib must start with #!/bin/bash"
    assert (
        "compatibility shim" in content.lower()
    ), "Shell lib must explain it is a compatibility shim"


def test_shell_lib_has_no_bash4_features() -> None:
    """Guard against associative arrays, indirect expansion, and [[ =~ ]].

    We strip comment lines before scanning so the prohibition list can be
    documented in the header comment without tripping the check.
    """
    raw = SHELL_LIB.read_text(encoding="utf-8")
    code_lines = [
        line for line in raw.splitlines() if not line.lstrip().startswith("#")
    ]
    code = "\n".join(code_lines)

    assert "declare -A" not in code, "No associative arrays (bash 4+)"
    assert "${!" not in code, "No indirect expansion (bash 4+)"
    assert "=~" not in code, "No regex match in [[ ]] (avoid for portability)"
    # Must not shell out to jq.
    assert " jq " not in code and "`jq" not in code and "$(jq" not in code


def test_shell_lib_has_validate_function() -> None:
    content = SHELL_LIB.read_text(encoding="utf-8")
    assert "validate_answer_json()" in content


def test_shell_lib_has_source_guard() -> None:
    content = SHELL_LIB.read_text(encoding="utf-8")
    assert "CODEPROBE_ANSWER_JSON_VERIFIER_LIB_SOURCED" in content


def test_validate_answer_json_success(tmp_path: Path) -> None:
    """A perfect file_list match should return exit 0 and write reward.txt=1.0."""
    task_dir = tmp_path / "task"
    _write_json(
        task_dir / "ground_truth.json",
        {
            "answer_type": "file_list",
            "answer": ["src/a.py", "src/b.py"],
            "confidence": 0.9,
        },
    )
    _write_json(task_dir / "answer.json", {"answer": ["src/a.py", "src/b.py"]})

    script = f'source "{SHELL_LIB}" && ' f'validate_answer_json "{task_dir}"'
    result = _run_bash(script, cwd=tmp_path)

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"

    reward_file = task_dir / "reward.txt"
    assert reward_file.is_file(), "reward.txt was not written"
    score_text = reward_file.read_text(encoding="utf-8").strip()
    score = float(score_text)
    assert score == pytest.approx(1.0)


def test_validate_answer_json_partial_score(tmp_path: Path) -> None:
    """A partial match should still exit 0 with a fractional score in reward.txt."""
    task_dir = tmp_path / "task"
    _write_json(
        task_dir / "ground_truth.json",
        {
            "answer_type": "file_list",
            "answer": ["a.py", "b.py", "c.py"],
            "confidence": 0.9,
        },
    )
    _write_json(task_dir / "answer.json", {"answer": ["a.py", "b.py"]})

    script = f'source "{SHELL_LIB}" && validate_answer_json "{task_dir}"'
    result = _run_bash(script, cwd=tmp_path)

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    reward_file = task_dir / "reward.txt"
    assert reward_file.is_file()
    score = float(reward_file.read_text(encoding="utf-8").strip())
    assert 0.0 < score < 1.0


def test_validate_answer_json_missing_dir(tmp_path: Path) -> None:
    """Calling with a non-existent dir should return a non-zero exit code."""
    bogus = tmp_path / "does_not_exist"
    script = f'source "{SHELL_LIB}" && validate_answer_json "{bogus}"'
    result = _run_bash(script, cwd=tmp_path)
    assert result.returncode != 0


def test_validate_answer_json_no_args(tmp_path: Path) -> None:
    script = f'source "{SHELL_LIB}" && validate_answer_json'
    result = _run_bash(script, cwd=tmp_path)
    assert result.returncode != 0


def test_source_guard_is_idempotent(tmp_path: Path) -> None:
    """Sourcing the lib twice should not fail or redefine behavior."""
    task_dir = tmp_path / "task"
    _write_json(
        task_dir / "ground_truth.json",
        {"answer_type": "boolean", "answer": True, "confidence": 0.9},
    )
    _write_json(task_dir / "answer.json", {"answer": True})

    script = (
        f'source "{SHELL_LIB}" && '
        f'source "{SHELL_LIB}" && '
        f'validate_answer_json "{task_dir}"'
    )
    result = _run_bash(script, cwd=tmp_path)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert (task_dir / "reward.txt").is_file()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_bash_posix_mode_compatibility(tmp_path: Path) -> None:
    """Run the script via plain bash to catch bash 4+ syntax regressions."""
    task_dir = tmp_path / "task"
    _write_json(
        task_dir / "ground_truth.json",
        {"answer_type": "count", "answer": 5, "confidence": 0.9},
    )
    _write_json(task_dir / "answer.json", {"answer": 5})

    script = f'set -u; source "{SHELL_LIB}"; validate_answer_json "{task_dir}"'
    result = _run_bash(script, cwd=tmp_path)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
