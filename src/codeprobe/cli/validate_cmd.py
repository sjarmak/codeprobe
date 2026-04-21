"""Validate command — checks structural validity of task directories."""

from __future__ import annotations

import json
import math
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


_MAX_GROUND_TRUTH_BYTES = 10 * 1024 * 1024  # 10 MB


def _check_ground_truth_dual(task_dir: Path) -> CheckResult:
    """Check dual-mode ground_truth.json: exists, parses, has 'answer' field.

    Dual verification uses the new ground-truth schema where the expected
    answer value is stored under the top-level ``answer`` key.  Also
    validates answer shape consistency and enforces a size limit.
    """
    from codeprobe.core.scoring import validate_ground_truth

    path = task_dir / "tests" / "ground_truth.json"
    if not path.exists():
        return CheckResult(
            name="tests/ground_truth.json exists",
            passed=False,
            detail="tests/ground_truth.json not found (required for dual mode)",
        )

    # Size guard
    try:
        file_size = path.stat().st_size
    except OSError as exc:
        return CheckResult(
            name="tests/ground_truth.json readable",
            passed=False,
            detail=f"cannot stat ground_truth.json: {exc}",
        )
    if file_size > _MAX_GROUND_TRUTH_BYTES:
        return CheckResult(
            name="tests/ground_truth.json size",
            passed=False,
            detail=(
                f"tests/ground_truth.json too large "
                f"({file_size} bytes, limit {_MAX_GROUND_TRUTH_BYTES})"
            ),
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

    if not isinstance(data, dict):
        return CheckResult(
            name="tests/ground_truth.json schema",
            passed=False,
            detail="tests/ground_truth.json must be a JSON object",
        )
    if "answer" not in data:
        return CheckResult(
            name="tests/ground_truth.json has answer",
            passed=False,
            detail="tests/ground_truth.json missing 'answer' field (required for dual mode)",
        )

    # Reuse scoring module's shape validation when the ground truth uses
    # a recognized format (v2 checks, v1 answer_type, or legacy expected).
    # Bare {"answer": ...} is valid for dual mode's basic check but has no
    # answer_type to validate against.
    if "checks" in data or "answer_type" in data or "expected" in data:
        validation_error = validate_ground_truth(data)
        if validation_error is not None:
            return CheckResult(
                name="tests/ground_truth.json schema",
                passed=False,
                detail=f"ground_truth.json schema error: {validation_error}",
            )

    return CheckResult(
        name="tests/ground_truth.json valid",
        passed=True,
        detail="ground_truth.json valid with 'answer' field",
    )


_VALID_SCORING_POLICIES: frozenset[str] = frozenset(
    {"", "min", "mean", "weighted", "gate"}
)


def _get_scoring_policy(meta: dict | None) -> str | None:
    """Extract scoring_policy from parsed metadata, if present.

    Returns None when the key is absent (caller may skip the check).
    """
    if meta is None:
        return None
    sp = meta.get("scoring_policy")
    if sp is None:
        verification = meta.get("verification", {})
        if isinstance(verification, dict):
            sp = verification.get("scoring_policy")
    return sp


def _parse_weight(
    meta: dict | None, key: str, default: float
) -> tuple[float, str | None]:
    """Extract and validate a weight_* float from parsed metadata.

    Returns ``(value, error_or_None)``. Invalid weights produce an error
    string instead of silently falling back to defaults.
    """
    if meta is None:
        return default, None
    val = meta.get(key)
    if val is None:
        verification = meta.get("verification", {})
        if isinstance(verification, dict):
            val = verification.get(key)
    if val is None:
        return default, None
    try:
        result = float(val)
    except (TypeError, ValueError):
        return default, f"invalid {key} value: {val!r}"
    if not math.isfinite(result):
        return default, f"non-finite {key}: {val!r}"
    if result < 0.0 or result > 1.0:
        return default, f"{key} out of range [0,1]: {result}"
    return result, None


def _check_scoring_policy(meta: dict | None) -> list[CheckResult]:
    """Validate scoring_policy and (for 'weighted') the weight sum.

    - scoring_policy must be one of {'', 'min', 'mean', 'weighted'}
    - if 'weighted', weight_direct + weight_artifact must equal 1.0 (+/- 1e-6)

    Returns an empty list when scoring_policy is absent from metadata
    (absent = use default of '' = valid; no policy check needed).
    """
    results: list[CheckResult] = []
    sp = _get_scoring_policy(meta)
    if sp is None:
        return results

    if sp not in _VALID_SCORING_POLICIES:
        results.append(
            CheckResult(
                name="scoring_policy valid",
                passed=False,
                detail=(
                    f"scoring_policy '{sp}' not in "
                    f"{sorted(_VALID_SCORING_POLICIES)}"
                ),
            )
        )
        return results

    results.append(
        CheckResult(
            name="scoring_policy valid",
            passed=True,
            detail=f"scoring_policy '{sp}' is valid",
        )
    )

    if sp == "weighted":
        wd, wd_err = _parse_weight(meta, "weight_direct", 0.5)
        wa, wa_err = _parse_weight(meta, "weight_artifact", 0.5)
        if wd_err:
            results.append(
                CheckResult(
                    name="weight_direct valid",
                    passed=False,
                    detail=wd_err,
                )
            )
        if wa_err:
            results.append(
                CheckResult(
                    name="weight_artifact valid",
                    passed=False,
                    detail=wa_err,
                )
            )
        if not wd_err and not wa_err:
            total = wd + wa
            if abs(total - 1.0) > 1e-6:
                results.append(
                    CheckResult(
                        name="weight sum",
                        passed=False,
                        detail=(
                            f"weight_direct ({wd}) + weight_artifact ({wa}) = "
                            f"{total}, expected 1.0 (+/- 1e-6)"
                        ),
                    )
                )
            else:
                results.append(
                    CheckResult(
                        name="weight sum",
                        passed=True,
                        detail=f"weight_direct + weight_artifact = {total}",
                    )
                )

    return results


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

    if vm == "dual":
        # Dual mode uses the new ground-truth schema with 'answer' field.
        results.append(_check_ground_truth_dual(task_dir))
    elif vm == "artifact_eval":
        results.append(_check_ground_truth(task_dir))

    # 6. Scoring policy / weight sum checks (always, regardless of mode)
    results.extend(_check_scoring_policy(meta))

    return results


def _looks_like_task_dir(path: Path) -> bool:
    """Return True iff *path* has the shape of a single task directory.

    A task directory has at least one of ``instruction.md``, ``task.toml``,
    or ``metadata.json`` at its root. Used to distinguish a single task
    from a "tasks parent" directory that contains many task subdirectories.
    """
    return (
        (path / "instruction.md").is_file()
        or (path / "task.toml").is_file()
        or (path / "metadata.json").is_file()
    )


def _list_child_task_dirs(path: Path) -> list[Path]:
    """Return immediate child directories of *path* that look like task dirs."""
    if not path.is_dir():
        return []
    return sorted(c for c in path.iterdir() if c.is_dir() and _looks_like_task_dir(c))


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

    Accepts either a single task directory or a parent directory that
    contains many task subdirectories (e.g. the ``.codeprobe/tasks``
    output of ``codeprobe mine``). When passed a parent, each child task
    is validated and a per-task summary plus overall totals is printed.
    """
    path = Path(task_dir).resolve()

    if strict:
        click.echo("NOTE: --strict: LLM spot-check not yet implemented")

    # Parent-of-tasks mode: only trigger when the arg itself is not a task
    # but its children look like tasks. Keeps legacy single-task semantics
    # intact for empty dirs and malformed inputs.
    if not _looks_like_task_dir(path):
        children = _list_child_task_dirs(path)
        if children:
            total = len(children)
            passed_count = 0
            for child in children:
                child_results = run_validate(child, strict=strict)
                child_ok = all(r.passed for r in child_results)
                marker = "PASS" if child_ok else "FAIL"
                click.echo(f"{marker}  {child.name}")
                if not child_ok:
                    for r in child_results:
                        if not r.passed:
                            click.echo(f"       FAIL  {r.name} ({r.detail})")
                if child_ok:
                    passed_count += 1
            click.echo()
            click.echo(
                f"Validated {total} task(s): {passed_count} passed, "
                f"{total - passed_count} failed."
            )
            if passed_count < total:
                raise SystemExit(1)
            return

    results = run_validate(path, strict=strict)

    any_failed = False
    for r in results:
        if r.passed:
            click.echo(f"  PASS  {r.name} ({r.detail})")
        else:
            any_failed = True
            click.echo(f"  FAIL  {r.name} ({r.detail})")

    if any_failed:
        raise SystemExit(1)
