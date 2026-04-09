"""Validate command — checks structural validity of task directories."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

import click

from codeprobe.models.task import TASK_TYPES, VERIFICATION_MODES


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


def _check_instruction(task_dir: Path) -> CheckResult:
    """Check that instruction.md exists and is non-empty."""
    path = task_dir / "instruction.md"
    if not path.exists():
        return CheckResult(
            name="instruction.md exists",
            passed=False,
            detail="instruction.md not found",
        )
    if path.stat().st_size == 0:
        return CheckResult(
            name="instruction.md exists",
            passed=False,
            detail="instruction.md is empty",
        )
    return CheckResult(
        name="instruction.md exists",
        passed=True,
        detail="instruction.md present and non-empty",
    )


def _load_metadata(task_dir: Path) -> tuple[CheckResult, dict | None]:
    """Check that task.toml or metadata.json exists and parses correctly.

    Returns the check result and the parsed metadata dict (or None on failure).
    """
    toml_path = task_dir / "task.toml"
    json_path = task_dir / "metadata.json"

    if toml_path.exists():
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib  # type: ignore[no-redef]

        try:
            with open(toml_path, "rb") as f:
                data = tomllib.load(f)
            return (
                CheckResult(
                    name="metadata parses",
                    passed=True,
                    detail="task.toml parsed successfully",
                ),
                data,
            )
        except Exception as exc:
            return (
                CheckResult(
                    name="metadata parses",
                    passed=False,
                    detail=f"task.toml parse error: {exc}",
                ),
                None,
            )

    if json_path.exists():
        try:
            with open(json_path) as f:
                data = json.load(f)
            return (
                CheckResult(
                    name="metadata parses",
                    passed=True,
                    detail="metadata.json parsed successfully",
                ),
                data,
            )
        except Exception as exc:
            return (
                CheckResult(
                    name="metadata parses",
                    passed=False,
                    detail=f"metadata.json parse error: {exc}",
                ),
                None,
            )

    return (
        CheckResult(
            name="metadata parses",
            passed=False,
            detail="neither task.toml nor metadata.json found",
        ),
        None,
    )


def _get_verification_mode(meta: dict | None) -> str | None:
    """Extract verification_mode from parsed metadata, if present."""
    if meta is None:
        return None
    # Could be nested under [verification] in TOML or top-level
    vm = meta.get("verification_mode")
    if vm is None:
        verification = meta.get("verification", {})
        if isinstance(verification, dict):
            vm = verification.get("verification_mode")
    return vm


def _get_task_type(meta: dict | None) -> str | None:
    """Extract task_type from parsed metadata, if present."""
    if meta is None:
        return None
    tt = meta.get("task_type")
    if tt is None:
        metadata_section = meta.get("metadata", {})
        if isinstance(metadata_section, dict):
            tt = metadata_section.get("task_type")
    return tt


def _check_test_script(task_dir: Path) -> CheckResult:
    """Check that tests/test.sh exists and is executable."""
    path = task_dir / "tests" / "test.sh"
    if not path.exists():
        return CheckResult(
            name="tests/test.sh exists",
            passed=False,
            detail="tests/test.sh not found",
        )
    mode = path.stat().st_mode
    if not (mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)):
        return CheckResult(
            name="tests/test.sh executable",
            passed=False,
            detail="tests/test.sh is not executable",
        )
    return CheckResult(
        name="tests/test.sh exists and executable",
        passed=True,
        detail="tests/test.sh present and executable",
    )


def _check_ground_truth(task_dir: Path) -> CheckResult:
    """Check that tests/ground_truth.json exists and has answer_type field."""
    path = task_dir / "tests" / "ground_truth.json"
    if not path.exists():
        return CheckResult(
            name="tests/ground_truth.json exists",
            passed=False,
            detail="tests/ground_truth.json not found",
        )
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as exc:
        return CheckResult(
            name="tests/ground_truth.json valid",
            passed=False,
            detail=f"tests/ground_truth.json parse error: {exc}",
        )

    if not isinstance(data, dict) or "answer_type" not in data:
        return CheckResult(
            name="tests/ground_truth.json has answer_type",
            passed=False,
            detail="tests/ground_truth.json missing 'answer_type' field",
        )
    return CheckResult(
        name="tests/ground_truth.json valid",
        passed=True,
        detail="ground_truth.json valid with answer_type",
    )


def _check_task_type(task_type: str) -> CheckResult:
    """Check that task_type is in the valid set."""
    if task_type in TASK_TYPES:
        return CheckResult(
            name="task_type valid",
            passed=True,
            detail=f"task_type '{task_type}' is valid",
        )
    return CheckResult(
        name="task_type valid",
        passed=False,
        detail=f"task_type '{task_type}' not in {sorted(TASK_TYPES)}",
    )


def _check_verification_mode(vm: str) -> CheckResult:
    """Check that verification_mode is in the valid set."""
    if vm in VERIFICATION_MODES:
        return CheckResult(
            name="verification_mode valid",
            passed=True,
            detail=f"verification_mode '{vm}' is valid",
        )
    return CheckResult(
        name="verification_mode valid",
        passed=False,
        detail=f"verification_mode '{vm}' not in {sorted(VERIFICATION_MODES)}",
    )


def run_validate(task_dir: Path, *, strict: bool = False) -> list[CheckResult]:
    """Run all structural validation checks on a task directory.

    Returns a list of CheckResult objects.
    """
    results: list[CheckResult] = []

    # 1. instruction.md
    results.append(_check_instruction(task_dir))

    # 2. Metadata file
    meta_result, meta = _load_metadata(task_dir)
    results.append(meta_result)

    # Extract verification_mode and task_type from metadata
    vm = _get_verification_mode(meta)
    task_type = _get_task_type(meta)

    # 3. task_type validation (if present)
    if task_type is not None:
        results.append(_check_task_type(task_type))

    # 4. verification_mode validation (if present)
    if vm is not None:
        results.append(_check_verification_mode(vm))

    # 5. Verification-mode-specific checks
    if vm in ("test_script", "dual") or vm is None:
        # Default assumption: test_script if no mode specified
        results.append(_check_test_script(task_dir))

    if vm in ("artifact_eval", "dual"):
        results.append(_check_ground_truth(task_dir))

    return results


@click.command("validate")
@click.argument("task_dir", type=click.Path(exists=True))
@click.option(
    "--strict",
    is_flag=True,
    default=False,
    help="Enable strict mode with LLM spot-check (placeholder).",
)
def validate(task_dir: str, strict: bool) -> None:
    """Validate structural correctness of a task directory.

    Checks that instruction.md, metadata, test scripts, and ground truth
    files are present and well-formed.
    """
    path = Path(task_dir).resolve()
    results = run_validate(path, strict=strict)

    if strict:
        click.echo("NOTE: --strict: LLM spot-check not yet implemented")

    any_failed = False
    for r in results:
        if r.passed:
            click.echo(f"  PASS  {r.name} ({r.detail})")
        else:
            any_failed = True
            click.echo(f"  FAIL  {r.name} ({r.detail})")

    if any_failed:
        raise SystemExit(1)
