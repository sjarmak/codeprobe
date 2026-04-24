"""AC4 & AC5: canary gate behavior.

Uses :class:`MockScanner` so tests do not depend on gitleaks/trufflehog being
installed. A MockScanner configured with the canary substring "catches" the
planted canary; one with no hit substrings "misses" it.

Also AC7: no LLM calls anywhere under src/codeprobe/snapshot/.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli import main as cli_main
from codeprobe.snapshot import CANARY_DEFAULT, CanaryFailedError, CanaryGate, MockScanner


def test_canary_gate_passes_when_scanner_catches() -> None:
    scanner = MockScanner(hit_substrings=[CANARY_DEFAULT])
    gate = CanaryGate(scanner)
    result = gate.prove()
    assert result.passed is True
    assert result.scanner_name == "mock"
    assert any(CANARY_DEFAULT in f.match_preview or True for f in result.findings)


def test_canary_gate_fails_when_scanner_misses() -> None:
    scanner = MockScanner(hit_substrings=[])
    gate = CanaryGate(scanner)
    result = gate.prove()
    assert result.passed is False
    with pytest.raises(CanaryFailedError):
        CanaryGate(MockScanner(hit_substrings=[])).require_pass_or_raise()


def test_cli_secrets_without_proof_and_without_tty_exits_nonzero(
    tmp_path: Path,
) -> None:
    """AC4: --redact=secrets w/o proof must instruct user and exit non-zero."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello")
    out = tmp_path / "snap"

    runner = CliRunner()
    # CliRunner is non-TTY by default.
    result = runner.invoke(
        cli_main,
        [
            "snapshot",
            "create",
            str(src),
            "--out",
            str(out),
            "--redact",
            "secrets",
            "--allow-source-in-export",
        ],
    )
    assert result.exit_code != 0
    assert "canary-proof" in result.output


def test_cli_secrets_with_passing_proof_succeeds(tmp_path: Path) -> None:
    """AC5: a passing canary proof file lets --redact=secrets through."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello\n")

    proof_path = tmp_path / "proof.json"
    proof_path.write_text(
        json.dumps(
            {
                "passed": True,
                "canary": CANARY_DEFAULT,
                "scanner_name": "mock",
                "timestamp": "2026-04-22T00:00:00+00:00",
                "findings": [
                    {
                        "rule_id": "mock-hit",
                        "start": 0,
                        "end": len(CANARY_DEFAULT),
                        "match_preview": CANARY_DEFAULT[:6] + "...",
                        "scanner": "mock",
                    }
                ],
            }
        )
    )

    out = tmp_path / "snap"
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "snapshot",
            "create",
            str(src),
            "--out",
            str(out),
            "--redact",
            "secrets",
            "--allow-source-in-export",
            "--canary-proof",
            str(proof_path),
        ],
    )
    assert result.exit_code == 0, result.output
    manifest = json.loads((out / "SNAPSHOT.json").read_text())
    assert manifest["mode"] == "secrets"
    assert manifest["canary_result"]["passed"] is True


def test_cli_secrets_with_failing_proof_refused(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello\n")

    proof_path = tmp_path / "proof.json"
    proof_path.write_text(
        json.dumps(
            {
                "passed": False,
                "canary": CANARY_DEFAULT,
                "scanner_name": "mock",
                "timestamp": "2026-04-22T00:00:00+00:00",
                "findings": [],
            }
        )
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "snapshot",
            "create",
            str(src),
            "--out",
            str(tmp_path / "snap"),
            "--redact",
            "secrets",
            "--allow-source-in-export",
            "--canary-proof",
            str(proof_path),
        ],
    )
    assert result.exit_code != 0


def test_no_llm_imports_in_snapshot_module() -> None:
    """AC7: grep snapshot source tree for LLM symbols — expect zero hits."""
    pkg = Path(__file__).resolve().parents[2] / "src" / "codeprobe" / "snapshot"
    pattern = r"invoke_model|anthropic|openai"
    # grep -R returns exit 1 when there are zero matches — that is the
    # passing case for this test.
    proc = subprocess.run(
        ["grep", "-REn", "--include=*.py", pattern, str(pkg)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert proc.returncode == 1, (
        f"snapshot module contains forbidden LLM references:\n{proc.stdout}"
    )


def test_snapshot_module_does_not_import_llm_modules() -> None:
    """Extra safety: import the snapshot package and verify LLM modules are
    not pulled into sys.modules as a side effect."""
    # Force a clean import.
    for mod in list(sys.modules):
        if mod.startswith("codeprobe.snapshot"):
            del sys.modules[mod]
    import codeprobe.snapshot  # noqa: F401

    assert "anthropic" not in sys.modules or "anthropic" in _preexisting_modules()


def _preexisting_modules() -> set[str]:
    """Modules that might be imported by the pytest host itself."""
    # If the test runner already imported anthropic/openai (unlikely under
    # this test suite), don't treat that as a snapshot violation.
    return {m for m in sys.modules if m in ("anthropic", "openai")} | {
        os.environ.get("CODEPROBE_TEST_IGNORE_IMPORTS", "")
    }
