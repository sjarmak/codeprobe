"""Contract tests for ``codeprobe calibrate``'s exit codes.

The calibrate module docstring advertises an exit-code contract to agent
callers.  It drifted once already (codeprobe-5v3n): it promised ``exit 1``
on gate rejection long after the v0.7 error-handler migration moved the
rejection path onto :class:`~codeprobe.cli.errors.DiagnosticError`, whose
frozen default exit code is ``2``.

These tests pin both halves of the contract:

* the *behaviour* — a rejecting holdout exits 2 with envelope error code
  ``CALIBRATION_REJECTED``; a passing holdout exits 0 and writes a profile;
* the *documentation* — the exit code named in the module docstring for the
  rejection path matches the frozen catalog entry in ``error_codes.json``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from click.testing import CliRunner

import codeprobe.cli.calibrate_cmd as calibrate_cmd
from codeprobe.cli import main

_CATALOG = Path(calibrate_cmd.__file__).parent / "error_codes.json"


def _catalog_exit_code(code: str) -> int:
    """Exit code the frozen error catalog assigns to ``code``."""
    catalog = json.loads(_CATALOG.read_text(encoding="utf-8"))
    for entry in catalog["codes"]:
        if entry["code"] == code:
            return int(entry["exit_code"])
    raise AssertionError(f"{code} is missing from {_CATALOG}")


def _parse_envelope(output: str) -> dict:
    """Return the last JSON envelope line from stdout, parsed as a dict."""
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("record_type") == "envelope":
            return payload
    raise AssertionError(f"no envelope line in output: {output!r}")


def _write_holdout(path: Path, *, n: int, repos: int = 3) -> Path:
    """Write a holdout whose two curators agree perfectly (Pearson == 1.0).

    Only the row count varies, so the gate outcome turns purely on
    ``--min-tasks`` rather than on correlation arithmetic.
    """
    rows = [
        {
            "task_id": f"t{i}",
            "curator_a": (i % 5) / 4.0,
            "curator_b": (i % 5) / 4.0,
            "repo": f"repo{i % repos}",
        }
        for i in range(n)
    ]
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def test_calibrate_rejection_exits_2_with_calibration_rejected(
    tmp_path: Path,
) -> None:
    """A holdout below --min-tasks is rejected: exit 2, CALIBRATION_REJECTED."""
    holdout = _write_holdout(tmp_path / "holdout.json", n=4)

    result = CliRunner().invoke(
        main,
        [
            "calibrate",
            str(holdout),
            "--curator-version",
            "v1",
            "--out",
            str(tmp_path / "profile.json"),
        ],
    )

    assert result.exit_code == _catalog_exit_code("CALIBRATION_REJECTED") == 2

    envelope = _parse_envelope(result.output)
    assert envelope["ok"] is False
    assert envelope["exit_code"] == 2
    assert envelope["error"]["code"] == "CALIBRATION_REJECTED"
    assert envelope["error"]["kind"] == "diagnostic"
    assert not (tmp_path / "profile.json").exists()


def test_calibrate_pass_exits_0_and_writes_profile(tmp_path: Path) -> None:
    """A holdout that clears the gate exits 0 and emits the profile."""
    holdout = _write_holdout(tmp_path / "holdout.json", n=12)
    out = tmp_path / "profile.json"

    result = CliRunner().invoke(
        main,
        [
            "calibrate",
            str(holdout),
            "--curator-version",
            "v1",
            "--min-tasks",
            "12",
            "--min-repos",
            "3",
            "--out",
            str(out),
        ],
    )

    assert result.exit_code == 0, result.output
    envelope = _parse_envelope(result.output)
    assert envelope["ok"] is True
    assert envelope["exit_code"] == 0
    assert out.exists()
    assert "calibration_confidence" in json.loads(out.read_text())


def test_calibrate_docstring_matches_the_frozen_exit_contract() -> None:
    """The documented rejection exit code must match the error catalog.

    Guards the exact drift this test module exists for: the docstring is the
    contract agent callers read, so a stale number there is a contract bug.
    """
    doc = calibrate_cmd.__doc__ or ""
    documented = {
        int(m) for m in re.findall(r"^\* ``(\d+)`` —", doc, flags=re.MULTILINE)
    }

    assert documented == {0, _catalog_exit_code("CALIBRATION_REJECTED")}
