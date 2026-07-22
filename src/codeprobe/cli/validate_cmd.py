"""Validate command — checks structural validity of task directories."""

from __future__ import annotations

import json
import math
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

import click

from codeprobe.cli._output_helpers import (
    add_json_flags,
    emit_envelope,
    resolve_explicit_mode,
)
from codeprobe.cli.envelope import WarningEntry
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
            import tomli as tomllib  # type: ignore[no-redef,import-not-found]

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


_TRUE_VALS: frozenset[str] = frozenset({"true", "yes", "1"})
_FALSE_VALS: frozenset[str] = frozenset({"false", "no", "0"})


def _read_ground_truth_data(task_dir: Path) -> dict | None:
    """Load ground_truth.json from either ``tests/`` or task root.

    Returns the parsed dict, or ``None`` when the file is missing or
    cannot be parsed as a JSON object. Mirrors the dual-layout fallback
    used by ``codeprobe.qa.verify._read_ground_truth`` so the validator
    handles both legacy mined tasks (root-level) and dual-mode synthetic
    tasks (``tests/`` subdirectory).
    """
    for candidate in (
        task_dir / "tests" / "ground_truth.json",
        task_dir / "ground_truth.json",
    ):
        if candidate.is_file():
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            if isinstance(payload, dict):
                return payload
    return None


def _expected_answer_from_gt(gt: dict) -> tuple[str | None, object]:
    """Pick the canonical answer + answer_type from a ground_truth payload.

    Returns ``(answer_type, answer_value)`` where ``answer_type`` is one of
    ``"file_list" | "symbol_list" | "count" | "boolean" | "string" |
    "list" | "scalar" | None``. ``answer_value`` carries whatever shape
    came out of the ground truth (list, int, bool, str, or ``None`` when
    nothing is recoverable). Resolution order:

    1. v1 schema: top-level ``answer_type`` + ``answer``.
    2. v2 schema: first ``checks[]`` entry with both fields populated.
    3. Legacy: top-level ``expected`` list (treated as ``list``-shaped),
       coupled with ``oracle_type`` if present (the mined-task convention).

    The function never raises — invalid shapes return ``(None, None)`` so
    the caller can skip the drift check rather than fail validation on a
    malformed ground truth (a separate check covers that).
    """
    if "answer_type" in gt and "answer" in gt:
        return gt.get("answer_type"), gt.get("answer")

    checks = gt.get("checks")
    if isinstance(checks, list):
        for check in checks:
            if not isinstance(check, dict):
                continue
            if "answer_type" in check and "answer" in check:
                return check.get("answer_type"), check.get("answer")

    if "expected" in gt and isinstance(gt["expected"], list):
        # Legacy mined-task layout. The oracle_type field is the closest
        # signal we have to the canonical answer_type.
        return gt.get("oracle_type") or "list", gt["expected"]

    return None, None


def _normalize_file_list(value: object) -> frozenset[str] | None:
    """Coerce an answer value into a frozenset of stripped file paths.

    Accepts either a list of strings or a multi-line text blob (the
    shape ``answer.txt`` always takes). Empty lines and ``#``-prefixed
    comments are dropped. Returns ``None`` when the input cannot be
    interpreted as a file list at all.
    """
    if isinstance(value, list):
        items = [str(p).strip() for p in value if p is not None]
        return frozenset(p for p in items if p)
    if isinstance(value, str):
        items = []
        for line in value.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            items.append(line)
        return frozenset(items)
    return None


def _normalize_count(value: object) -> int | None:
    """Coerce an answer value into an int, or ``None`` on parse failure."""
    if isinstance(value, bool):
        return None  # bool is an int subclass in Python; reject explicitly
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        for line in value.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                return int(line)
            except ValueError:
                return None
    return None


def _normalize_bool(value: object) -> bool | None:
    """Coerce an answer value into a bool, or ``None`` when unrecognized."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        for line in value.splitlines():
            line = line.strip().lower()
            if not line or line.startswith("#"):
                continue
            if line in _TRUE_VALS:
                return True
            if line in _FALSE_VALS:
                return False
            return None
    return None


def _normalize_scalar(value: object) -> str | None:
    """Coerce a scalar answer to a stripped string for equality compare."""
    if value is None:
        return None
    if isinstance(value, list):
        return None  # caller should have routed this through file_list
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    return None


def _check_answer_txt_drift(task_dir: Path) -> CheckResult | None:
    """Compare a curator-shipped ``answer.txt`` against ground_truth.

    This guards against the regression first observed against
    ``e2e-codeprobe-self/tasks/0f2b0737`` (codeprobe-w8pg): a mined task
    shipped an ``answer.txt`` that drifted out of sync with its
    ``ground_truth.json``. The calibration triad's golden synthesizer
    already prefers ground_truth (codeprobe-9fri), but downstream tools
    that read ``answer.txt`` directly can still trip on stale data.

    Returns:
        - ``None`` when ``answer.txt`` is absent (the file is optional).
        - ``CheckResult`` with ``passed=True`` and a "warn:" detail
          prefix when drift is detected. Drift is advisory rather than
          fatal so this check follows the same pattern as the QA-warning
          plumbing in ``_check_qa``.
        - ``CheckResult`` with ``passed=True`` and a normal detail when
          the two agree (or when ground_truth offers no comparable
          answer to score against).
    """
    answer_path = task_dir / "answer.txt"
    if not answer_path.is_file():
        return None

    gt = _read_ground_truth_data(task_dir)
    if gt is None:
        return CheckResult(
            name="answer.txt drift",
            passed=True,
            detail="info: answer.txt present but ground_truth.json unavailable",
        )

    answer_type, expected = _expected_answer_from_gt(gt)
    if expected is None:
        return CheckResult(
            name="answer.txt drift",
            passed=True,
            detail=(
                "info: answer.txt present but ground_truth has no comparable "
                "answer/expected/checks field"
            ),
        )

    try:
        answer_text = answer_path.read_text(encoding="utf-8")
    except OSError as exc:
        return CheckResult(
            name="answer.txt drift",
            passed=True,
            detail=f"info: failed to read answer.txt for drift check: {exc}",
        )

    list_types = {"file_list", "symbol_list", "list"}
    if answer_type in list_types or isinstance(expected, list):
        gt_set = _normalize_file_list(expected)
        ans_set = _normalize_file_list(answer_text)
        if gt_set is None or ans_set is None:
            return CheckResult(
                name="answer.txt drift",
                passed=True,
                detail=(
                    f"info: answer.txt drift skipped — could not normalize "
                    f"answer_type={answer_type!r}"
                ),
            )
        if gt_set == ans_set:
            return CheckResult(
                name="answer.txt drift",
                passed=True,
                detail=f"answer.txt matches ground_truth ({len(gt_set)} items)",
            )
        only_gt = sorted(gt_set - ans_set)
        only_ans = sorted(ans_set - gt_set)
        intersect = len(gt_set & ans_set)
        union = len(gt_set | ans_set)
        jaccard = (intersect / union) if union else 0.0
        return CheckResult(
            name="answer.txt drift",
            passed=True,
            detail=(
                f"warn: answer.txt disagrees with ground_truth "
                f"(jaccard={jaccard:.2f}, in gt only: {len(only_gt)}, "
                f"in answer.txt only: {len(only_ans)})"
            ),
        )

    if answer_type == "count":
        gt_val = _normalize_count(expected)
        ans_val = _normalize_count(answer_text)
        if gt_val is None or ans_val is None:
            return CheckResult(
                name="answer.txt drift",
                passed=True,
                detail="info: answer.txt drift skipped — count parse failed",
            )
        if gt_val == ans_val:
            return CheckResult(
                name="answer.txt drift",
                passed=True,
                detail=f"answer.txt matches ground_truth (count={gt_val})",
            )
        return CheckResult(
            name="answer.txt drift",
            passed=True,
            detail=(
                f"warn: answer.txt disagrees with ground_truth "
                f"(answer.txt={ans_val}, ground_truth={gt_val})"
            ),
        )

    if answer_type == "boolean":
        gt_val = _normalize_bool(expected)
        ans_val = _normalize_bool(answer_text)
        if gt_val is None or ans_val is None:
            return CheckResult(
                name="answer.txt drift",
                passed=True,
                detail="info: answer.txt drift skipped — boolean parse failed",
            )
        if gt_val == ans_val:
            return CheckResult(
                name="answer.txt drift",
                passed=True,
                detail=f"answer.txt matches ground_truth (boolean={gt_val})",
            )
        return CheckResult(
            name="answer.txt drift",
            passed=True,
            detail=(
                f"warn: answer.txt disagrees with ground_truth "
                f"(answer.txt={ans_val}, ground_truth={gt_val})"
            ),
        )

    gt_str = _normalize_scalar(expected)
    ans_str = _normalize_scalar(answer_text)
    if gt_str is None or ans_str is None:
        return CheckResult(
            name="answer.txt drift",
            passed=True,
            detail=(
                f"info: answer.txt drift skipped — unsupported "
                f"answer_type={answer_type!r}"
            ),
        )
    if gt_str == ans_str:
        return CheckResult(
            name="answer.txt drift",
            passed=True,
            detail="answer.txt matches ground_truth (scalar)",
        )
    return CheckResult(
        name="answer.txt drift",
        passed=True,
        detail=(
            "warn: answer.txt disagrees with ground_truth "
            f"(answer.txt={ans_str!r}, ground_truth={gt_str!r})"
        ),
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


def _check_qa(task_dir: Path) -> list[CheckResult]:
    """Run benchmark_qa_core checks, mapping each finding to a CheckResult.

    Findings of severity ``error`` produce a failed CheckResult; ``warning``
    and ``info`` findings surface as passed CheckResults whose detail
    string starts with ``"warn: "`` / ``"info: "`` so the UI signals the
    advisory level without aborting.

    Class-D (path scope) findings are suppressed inside ``verify_task_qa``
    by default — codeprobe's scoring contract already weights off-scope
    answers via reward, so re-emitting them as QA errors creates noise.
    """
    from codeprobe.qa.verify import verify_task_qa

    qa_result = verify_task_qa(task_dir)
    results: list[CheckResult] = []
    for finding in qa_result.findings:
        loc = f" [{finding.location}]" if finding.location else ""
        if finding.severity == "error":
            results.append(
                CheckResult(
                    name=f"qa:{finding.code}",
                    passed=False,
                    detail=f"{finding.message}{loc}",
                )
            )
        else:
            results.append(
                CheckResult(
                    name=f"qa:{finding.code}",
                    passed=True,
                    detail=f"{finding.severity}: {finding.message}{loc}",
                )
            )
    if not qa_result.findings:
        results.append(
            CheckResult(
                name="qa checks",
                passed=True,
                detail="benchmark_qa_core: 0 findings",
            )
        )
    return results


def run_validate(
    task_dir: Path, *, strict: bool = False, qa: bool = False
) -> list[CheckResult]:
    """Run all structural validation checks on a task directory.

    When ``qa=True``, also run the ``benchmark_qa_core`` lib's three
    schema-agnostic checks (oracle coherence, scoring honesty, aux-file
    leakage) and append one CheckResult per finding. Off by default to
    keep the legacy validate semantics intact for callers that haven't
    opted in.

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

    # 7. answer.txt drift check (silent skip when answer.txt is absent).
    drift_result = _check_answer_txt_drift(task_dir)
    if drift_result is not None:
        results.append(drift_result)

    # 8. QA library checks — opt-in.
    if qa:
        results.extend(_check_qa(task_dir))

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
@add_json_flags
@click.argument("task_dir", type=click.Path(exists=True))
@click.option(
    "--strict",
    is_flag=True,
    default=False,
    help="Enable strict mode with LLM spot-check (placeholder).",
)
@click.option(
    "--qa",
    is_flag=True,
    default=False,
    help=(
        "Also run benchmark_qa_core checks (oracle coherence, scoring "
        "honesty, aux-file leakage)."
    ),
)
def validate(
    task_dir: str,
    strict: bool,
    qa: bool,
    json_flag: bool,
    no_json_flag: bool,
    json_lines_flag: bool,
) -> None:
    """Validate structural correctness of a task directory.

    Checks that instruction.md, metadata, test scripts, and ground truth
    files are present and well-formed.

    Accepts either a single task directory or a parent directory that
    contains many task subdirectories (e.g. the ``.codeprobe/tasks``
    output of ``codeprobe mine``). When passed a parent, each child task
    is validated and a per-task summary plus overall totals is printed.

    With ``--json``, emits a single machine-readable envelope on stdout
    instead of the pretty report; the exit code is unchanged (non-zero
    iff any check failed).
    """
    mode = resolve_explicit_mode(
        "validate", json_flag, no_json_flag, json_lines_flag
    )
    emit_json = mode.mode in ("single_envelope", "ndjson")
    path = Path(task_dir).resolve()

    warnings: list[WarningEntry] = []
    if strict:
        if emit_json:
            warnings.append(
                WarningEntry(
                    code="STRICT_NOT_IMPLEMENTED",
                    message="--strict: LLM spot-check not yet implemented",
                )
            )
        else:
            click.echo("NOTE: --strict: LLM spot-check not yet implemented")

    # Parent-of-tasks mode: only trigger when the arg itself is not a task
    # but its children look like tasks. Keeps legacy single-task semantics
    # intact for empty dirs and malformed inputs.
    if not _looks_like_task_dir(path):
        children = _list_child_task_dirs(path)
        if children:
            total = len(children)
            passed_count = 0
            task_rows: list[dict[str, object]] = []
            for child in children:
                child_results = run_validate(child, strict=strict, qa=qa)
                child_ok = all(r.passed for r in child_results)
                if child_ok:
                    passed_count += 1
                if emit_json:
                    task_rows.append(
                        {
                            "task_id": child.name,
                            "passed": child_ok,
                            "failed_checks": [
                                asdict(r)
                                for r in child_results
                                if not r.passed
                            ],
                        }
                    )
                    continue
                marker = "PASS" if child_ok else "FAIL"
                click.echo(f"{marker}  {child.name}")
                if not child_ok:
                    for r in child_results:
                        if not r.passed:
                            click.echo(f"       FAIL  {r.name} ({r.detail})")
            failed_count = total - passed_count
            if emit_json:
                emit_envelope(
                    command="validate",
                    ok=failed_count == 0,
                    exit_code=1 if failed_count else 0,
                    data={
                        "task_dir": str(path),
                        "tasks": task_rows,
                        "total": total,
                        "passed_count": passed_count,
                        "failed_count": failed_count,
                    },
                    warnings=warnings,
                )
            else:
                click.echo()
                click.echo(
                    f"Validated {total} task(s): {passed_count} passed, "
                    f"{failed_count} failed."
                )
            if failed_count:
                # lint-exempt: report/envelope already rendered; exit code only.
                raise SystemExit(1)
            return

    results = run_validate(path, strict=strict, qa=qa)
    failed_count = sum(1 for r in results if not r.passed)

    if emit_json:
        emit_envelope(
            command="validate",
            ok=failed_count == 0,
            exit_code=1 if failed_count else 0,
            data={
                "task_dir": str(path),
                "checks": [asdict(r) for r in results],
                "passed_count": len(results) - failed_count,
                "failed_count": failed_count,
            },
            warnings=warnings,
        )
    else:
        for r in results:
            marker = "PASS" if r.passed else "FAIL"
            click.echo(f"  {marker}  {r.name} ({r.detail})")

    if failed_count:
        # lint-exempt: report/envelope already rendered; exit code only.
        raise SystemExit(1)
