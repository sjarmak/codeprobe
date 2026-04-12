"""Acceptance criteria verifier.

Evaluates a list of :class:`~acceptance.loader.Criterion` objects against a
Test Agent workspace directory and produces a ``verdict.json`` summary.

The verifier implements a three-tier evaluation model:

- **structural** — Python introspection and source-file inspection. These
  checks run without a workspace and are effectively instant.
- **behavioral** — CLI commands and output inspection. These require a
  workspace directory where captured command outputs live.
- **statistical** — aggregate assertions over workspace artifacts
  (``results.json`` counts, cost fields, canary UUIDs, etc.).

## Per-tier evaluation thresholds (premortem RISK-1)

A naive flat threshold ("at least 80% of all criteria must be evaluated")
lets statistical checks silently skip while structural checks inflate the
evaluated total. The verifier instead enforces an *independent* threshold per
tier: each tier must reach 80% evaluated (pass + fail, ignoring skip) for the
verdict to be marked ``EVALUATED``. Otherwise the verdict is ``INCOMPLETE``
and ``all_pass`` is always ``False`` — callers treat this as "try again" or
"fix the verifier", never as "passed".

## Skip semantics

Criteria are skipped (rather than failed) when:

- The check_type has no handler registered.
- A workspace artifact referenced by the params is missing.
- An optional pre-check cannot run (e.g. canary.txt missing).

Skipping is information, not failure — the whole point of tracking
``evaluated_pct`` per tier is to surface silent under-evaluation.

## Canary detection

If ``workspace/canary.txt`` exists, its contents are treated as a sentinel
UUID. The verifier searches every other file in the workspace for that
UUID; the check passes only if at least one file contains it. Missing
``canary.txt`` results in a skip, not a failure, because callers may choose
to run without canary injection.

See ``prd_behavioral_acceptance_loop.md`` for the contract this module
implements and ``docs/prd/`` for the PRDs the criteria manifest encodes.
"""

from __future__ import annotations

import importlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from acceptance.loader import ALLOWED_TIERS, Criterion, load_criteria

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Minimum percentage of criteria per tier that must be evaluated (pass or
#: fail, not skip) for the verdict to be considered valid. Per-tier, not
#: flat — see module docstring.
MIN_TIER_EVALUATED_PCT: float = 80.0

#: Default per-command timeout for behavioral checks, in seconds. Kept small
#: so a hung subprocess cannot stall the acceptance loop indefinitely.
DEFAULT_COMMAND_TIMEOUT_S: float = 30.0

#: File inside the workspace whose contents are interpreted as the canary
#: UUID for :func:`Verifier._check_canary`.
CANARY_FILENAME: str = "canary.txt"

#: Possible verdict statuses.
STATUS_EVALUATED: str = "EVALUATED"
STATUS_INCOMPLETE: str = "INCOMPLETE"

#: Per-criterion result values returned by handler methods.
RESULT_PASS: str = "pass"
RESULT_FAIL: str = "fail"
RESULT_SKIP: str = "skip"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """Immutable result of evaluating a single criterion."""

    criterion_id: str
    tier: str
    severity: str
    result: str  # one of RESULT_PASS | RESULT_FAIL | RESULT_SKIP
    evidence: str


HandlerFn = Callable[["Verifier", Criterion], CheckResult]


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------


class Verifier:
    """Evaluate acceptance criteria against a workspace directory.

    Args:
        criteria_path: Path to a ``criteria.toml`` manifest. Parsed eagerly
            at construction so malformed manifests fail loudly before any
            workspace work begins.
        project_root: Optional path to the codeprobe project root. Used by
            structural checks that read source files (e.g. ``regex_present``)
            without a workspace. Defaults to the parent of the criteria file.
    """

    def __init__(
        self,
        criteria_path: Path | str,
        project_root: Path | str | None = None,
    ) -> None:
        self.criteria_path = Path(criteria_path).resolve()
        self.criteria: list[Criterion] = load_criteria(self.criteria_path)
        if project_root is not None:
            self.project_root = Path(project_root).resolve()
        else:
            # criteria.toml lives at acceptance/criteria.toml; project root
            # is two levels up.
            self.project_root = self.criteria_path.parent.parent

    # ------------------------------------------------------------------ run

    def run(
        self,
        workspace: Path | str,
        iteration: int = 1,
    ) -> dict[str, Any]:
        """Evaluate every criterion and return a verdict dictionary.

        Args:
            workspace: Directory containing captured command outputs from a
                Test Agent run. May be empty — missing artifacts cause a
                skip, not a failure.
            iteration: Iteration number for the enclosing acceptance loop.
                Recorded in the verdict for traceability.

        Returns:
            A verdict dict matching the schema in the module docstring of
            ``acceptance/verify.py``.
        """
        workspace_path = Path(workspace).resolve()
        workspace_path.mkdir(parents=True, exist_ok=True)

        results: list[CheckResult] = []
        for criterion in self.criteria:
            handler = self._handlers().get(criterion.check_type)
            if handler is None:
                results.append(
                    CheckResult(
                        criterion_id=criterion.id,
                        tier=criterion.tier,
                        severity=criterion.severity,
                        result=RESULT_SKIP,
                        evidence=(f"unsupported check_type: {criterion.check_type!r}"),
                    )
                )
                continue
            try:
                results.append(handler(self, criterion, workspace_path))
            except Exception as exc:  # pragma: no cover - defensive
                results.append(
                    CheckResult(
                        criterion_id=criterion.id,
                        tier=criterion.tier,
                        severity=criterion.severity,
                        result=RESULT_SKIP,
                        evidence=f"handler raised {type(exc).__name__}: {exc}",
                    )
                )

        verdict = self._build_verdict(
            results=results,
            workspace=workspace_path,
            iteration=iteration,
        )
        return verdict

    def write_verdict(
        self,
        verdict: dict[str, Any],
        out_path: Path | str,
    ) -> Path:
        """Write ``verdict`` as indented JSON to ``out_path`` and return it.

        The parent directory is created if necessary so callers can pass a
        workspace-relative path without precomputing directories.
        """
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(verdict, indent=2, sort_keys=True))
        return path

    # ------------------------------------------------------------ aggregation

    def _build_verdict(
        self,
        results: list[CheckResult],
        workspace: Path,
        iteration: int,
    ) -> dict[str, Any]:
        tier_counts: dict[str, dict[str, int]] = {
            tier: {"total": 0, "pass": 0, "fail": 0, "skip": 0}
            for tier in sorted(ALLOWED_TIERS)
        }
        pass_count = 0
        fail_count = 0
        skip_count = 0
        failures: list[dict[str, str]] = []

        for res in results:
            bucket = tier_counts[res.tier]
            bucket["total"] += 1
            bucket[res.result] += 1
            if res.result == RESULT_PASS:
                pass_count += 1
            elif res.result == RESULT_FAIL:
                fail_count += 1
                failures.append(
                    {
                        "criterion_id": res.criterion_id,
                        "tier": res.tier,
                        "severity": res.severity,
                        "evidence": res.evidence,
                    }
                )
            else:
                skip_count += 1

        evaluated_pct: dict[str, float] = {}
        for tier, counts in tier_counts.items():
            total = counts["total"]
            if total == 0:
                evaluated_pct[tier] = 0.0
                continue
            evaluated = counts["pass"] + counts["fail"]
            evaluated_pct[tier] = round(evaluated / total * 100.0, 2)

        # Status rule: any tier with >=1 criterion below threshold ⇒
        # INCOMPLETE. Empty tiers (total == 0) are ignored so an unused tier
        # never blocks a valid verdict.
        incomplete_tiers = [
            tier
            for tier, counts in tier_counts.items()
            if counts["total"] >= 1 and evaluated_pct[tier] < MIN_TIER_EVALUATED_PCT
        ]
        status = STATUS_INCOMPLETE if incomplete_tiers else STATUS_EVALUATED
        all_pass = status == STATUS_EVALUATED and fail_count == 0

        return {
            "iteration": iteration,
            "workspace": str(workspace),
            "criteria_source": str(self.criteria_path),
            "pass_count": pass_count,
            "fail_count": fail_count,
            "skip_count": skip_count,
            "total_criteria": len(results),
            "evaluated_pct": evaluated_pct,
            "tier_counts": tier_counts,
            "status": status,
            "all_pass": all_pass,
            "failures": failures,
            "evaluated_at": datetime.now(UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
        }

    # ------------------------------------------------------------- dispatch

    @staticmethod
    def _handlers() -> dict[str, Callable[[Verifier, Criterion, Path], CheckResult]]:
        return {
            # Structural (Python introspection / source inspection).
            "import_equals": Verifier._check_import_equals,
            "dataclass_has_fields": Verifier._check_dataclass_has_fields,
            "regex_present": Verifier._check_regex_present,
            "regex_absent": Verifier._check_regex_absent,
            "pyproject_deps_bounded": Verifier._check_pyproject_deps_bounded,
            # Behavioral (CLI outputs from workspace).
            "cli_exit_code": Verifier._check_cli_exit_code,
            "cli_help_contains": Verifier._check_cli_help_contains,
            "cli_stdout_contains": Verifier._check_cli_stdout_contains,
            "cli_writes_file": Verifier._check_cli_writes_file,
            "file_exists": Verifier._check_file_exists,
            "stdout_contains": Verifier._check_stdout_contains,
            "stderr_contains": Verifier._check_stderr_contains,
            # Statistical (aggregate over workspace artifacts).
            "count_ge": Verifier._check_count_ge,
            "json_count_ge": Verifier._check_json_count_ge,
            "json_field_not_null": Verifier._check_json_field_not_null,
            "json_field_equals": Verifier._check_json_field_equals,
            "json_field_type": Verifier._check_json_field_type,
            "canary_detect": Verifier._check_canary,
        }

    # ------------------------------------------------------------- helpers

    def _resolve_project_file(self, rel: str) -> Path:
        """Resolve a project-relative path under :attr:`project_root`."""
        return (self.project_root / rel).resolve()

    @staticmethod
    def _skip(criterion: Criterion, evidence: str) -> CheckResult:
        return CheckResult(
            criterion_id=criterion.id,
            tier=criterion.tier,
            severity=criterion.severity,
            result=RESULT_SKIP,
            evidence=evidence,
        )

    @staticmethod
    def _pass(criterion: Criterion, evidence: str) -> CheckResult:
        return CheckResult(
            criterion_id=criterion.id,
            tier=criterion.tier,
            severity=criterion.severity,
            result=RESULT_PASS,
            evidence=evidence,
        )

    @staticmethod
    def _fail(criterion: Criterion, evidence: str) -> CheckResult:
        return CheckResult(
            criterion_id=criterion.id,
            tier=criterion.tier,
            severity=criterion.severity,
            result=RESULT_FAIL,
            evidence=evidence,
        )

    # ------------------------------------------------ structural handlers

    def _check_import_equals(
        self, criterion: Criterion, _workspace: Path
    ) -> CheckResult:
        params = criterion.params
        module_name = params.get("module")
        symbol = params.get("symbol")
        expected = params.get("expected")
        if not module_name or not symbol:
            return self._skip(criterion, "missing module/symbol params")
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            return self._skip(criterion, f"cannot import {module_name}: {exc}")
        if not hasattr(module, symbol):
            return self._fail(criterion, f"{module_name}.{symbol} not defined")
        actual = getattr(module, symbol)
        if actual == expected:
            return self._pass(criterion, f"{module_name}.{symbol} == {expected!r}")
        return self._fail(
            criterion,
            f"{module_name}.{symbol} == {actual!r}, expected {expected!r}",
        )

    def _check_dataclass_has_fields(
        self, criterion: Criterion, _workspace: Path
    ) -> CheckResult:
        params = criterion.params
        module_name = params.get("module")
        symbol = params.get("symbol")
        required = params.get("required_fields") or []
        if not module_name or not symbol or not required:
            return self._skip(criterion, "missing module/symbol/required_fields params")
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            return self._skip(criterion, f"cannot import {module_name}: {exc}")
        cls = getattr(module, symbol, None)
        if cls is None:
            return self._fail(criterion, f"{module_name}.{symbol} not defined")
        # dataclasses.fields works for dataclasses; fall back to __annotations__
        # for other class shapes (NamedTuple, Protocol with attributes).
        try:
            from dataclasses import fields

            present = {f.name for f in fields(cls)}
        except TypeError:
            present = set(getattr(cls, "__annotations__", {}).keys())
        missing = [f for f in required if f not in present]
        if missing:
            return self._fail(
                criterion,
                f"{module_name}.{symbol} missing fields: {missing}",
            )
        return self._pass(
            criterion,
            f"{module_name}.{symbol} has all required fields",
        )

    def _check_regex_present(
        self, criterion: Criterion, _workspace: Path
    ) -> CheckResult:
        params = criterion.params
        file_rel = params.get("file")
        pattern = params.get("pattern")
        forbid = params.get("forbid_pattern")
        if not file_rel or not pattern:
            return self._skip(criterion, "missing file/pattern params")
        file_path = self._resolve_project_file(file_rel)
        if not file_path.is_file():
            return self._skip(criterion, f"file not found: {file_rel}")
        text = file_path.read_text(errors="replace")
        if not re.search(pattern, text):
            return self._fail(criterion, f"pattern {pattern!r} not found in {file_rel}")
        if forbid and re.search(forbid, text):
            return self._fail(
                criterion,
                f"forbidden pattern {forbid!r} present in {file_rel}",
            )
        return self._pass(criterion, f"pattern {pattern!r} present in {file_rel}")

    def _check_regex_absent(
        self, criterion: Criterion, _workspace: Path
    ) -> CheckResult:
        params = criterion.params
        file_rel = params.get("file")
        forbid = params.get("forbid_pattern")
        if not file_rel or not forbid:
            return self._skip(criterion, "missing file/forbid_pattern params")
        file_path = self._resolve_project_file(file_rel)
        if not file_path.is_file():
            return self._skip(criterion, f"file not found: {file_rel}")
        text = file_path.read_text(errors="replace")
        if re.search(forbid, text, re.MULTILINE):
            return self._fail(
                criterion,
                f"forbidden pattern {forbid!r} present in {file_rel}",
            )
        return self._pass(
            criterion, f"forbidden pattern {forbid!r} absent from {file_rel}"
        )

    def _check_pyproject_deps_bounded(
        self, criterion: Criterion, _workspace: Path
    ) -> CheckResult:
        params = criterion.params
        file_rel = params.get("file", "pyproject.toml")
        file_path = self._resolve_project_file(file_rel)
        if not file_path.is_file():
            return self._skip(criterion, f"file not found: {file_rel}")
        import tomllib

        with file_path.open("rb") as fh:
            data = tomllib.load(fh)
        deps: list[str] = list(data.get("project", {}).get("dependencies", []) or [])
        optional = data.get("project", {}).get("optional-dependencies", {}) or {}
        for group in optional.values():
            deps.extend(group)
        unbounded = [d for d in deps if "<" not in d and not d.strip().startswith("#")]
        if unbounded:
            return self._fail(
                criterion,
                f"unbounded deps in {file_rel}: {unbounded[:5]}"
                + ("..." if len(unbounded) > 5 else ""),
            )
        return self._pass(criterion, f"all deps in {file_rel} declare upper bounds")

    # ------------------------------------------------ behavioral handlers

    def _check_cli_exit_code(
        self, criterion: Criterion, workspace: Path
    ) -> CheckResult:
        """Inspect a captured command's exit code from the workspace.

        Behavioral checks do NOT spawn subprocesses from the verifier — the
        Test Agent is expected to run commands and record their output in
        the workspace. Exit codes live in ``<workspace>/<criterion_id>.exit``
        and stdout/stderr live alongside. Missing files → skip.
        """
        expected = criterion.params.get("expected_exit")
        expected_not = criterion.params.get("expected_exit_not")
        exit_file = workspace / f"{criterion.id}.exit"
        if not exit_file.is_file():
            return self._skip(
                criterion, f"exit-code artifact missing: {exit_file.name}"
            )
        try:
            actual = int(exit_file.read_text().strip())
        except ValueError:
            return self._skip(
                criterion,
                f"exit-code artifact not parseable: {exit_file.name}",
            )
        if expected is not None:
            if actual == expected:
                return self._pass(criterion, f"exit code == {expected}")
            return self._fail(criterion, f"exit code {actual}, expected {expected}")
        if expected_not is not None:
            if actual != expected_not:
                return self._pass(criterion, f"exit code {actual} != {expected_not}")
            return self._fail(
                criterion,
                f"exit code {actual} matched forbidden {expected_not}",
            )
        return self._skip(criterion, "no expected_exit or expected_exit_not param")

    def _check_cli_help_contains(
        self, criterion: Criterion, workspace: Path
    ) -> CheckResult:
        """All captured help outputs must contain ``must_contain``."""
        must_contain = criterion.params.get("must_contain")
        if not must_contain:
            return self._skip(criterion, "missing must_contain param")
        stdout_file = workspace / f"{criterion.id}.stdout"
        if not stdout_file.is_file():
            return self._skip(criterion, f"stdout artifact missing: {stdout_file.name}")
        text = stdout_file.read_text(errors="replace")
        if must_contain in text:
            return self._pass(
                criterion, f"{must_contain!r} found in {stdout_file.name}"
            )
        return self._fail(criterion, f"{must_contain!r} not in {stdout_file.name}")

    def _check_cli_stdout_contains(
        self, criterion: Criterion, workspace: Path
    ) -> CheckResult:
        return self._stdout_substring_check(criterion, workspace)

    def _check_stdout_contains(
        self, criterion: Criterion, workspace: Path
    ) -> CheckResult:
        return self._stdout_substring_check(criterion, workspace)

    def _stdout_substring_check(
        self, criterion: Criterion, workspace: Path
    ) -> CheckResult:
        must_contain = criterion.params.get("must_contain") or criterion.params.get(
            "substring"
        )
        if not must_contain:
            return self._skip(criterion, "missing must_contain param")
        stdout_file = workspace / f"{criterion.id}.stdout"
        if not stdout_file.is_file():
            return self._skip(criterion, f"stdout artifact missing: {stdout_file.name}")
        text = stdout_file.read_text(errors="replace")
        if must_contain in text:
            return self._pass(criterion, f"{must_contain!r} found in stdout")
        return self._fail(criterion, f"{must_contain!r} not in stdout")

    def _check_stderr_contains(
        self, criterion: Criterion, workspace: Path
    ) -> CheckResult:
        must_contain = criterion.params.get("must_contain") or criterion.params.get(
            "substring"
        )
        if not must_contain:
            return self._skip(criterion, "missing must_contain param")
        stderr_file = workspace / f"{criterion.id}.stderr"
        if not stderr_file.is_file():
            return self._skip(criterion, f"stderr artifact missing: {stderr_file.name}")
        text = stderr_file.read_text(errors="replace")
        if must_contain in text:
            return self._pass(criterion, f"{must_contain!r} found in stderr")
        return self._fail(criterion, f"{must_contain!r} not in stderr")

    def _check_cli_writes_file(
        self, criterion: Criterion, workspace: Path
    ) -> CheckResult:
        rel = criterion.params.get("expected_path")
        if not rel:
            return self._skip(criterion, "missing expected_path param")
        candidate = workspace / rel
        if candidate.exists():
            return self._pass(criterion, f"file present: {rel}")
        return self._fail(criterion, f"file missing: {rel}")

    def _check_file_exists(self, criterion: Criterion, workspace: Path) -> CheckResult:
        rel = criterion.params.get("path") or criterion.params.get("expected_path")
        if not rel:
            return self._skip(criterion, "missing path param")
        candidate = workspace / rel
        if candidate.exists():
            return self._pass(criterion, f"file present: {rel}")
        return self._fail(criterion, f"file missing: {rel}")

    # ------------------------------------------------ statistical handlers

    def _check_count_ge(self, criterion: Criterion, workspace: Path) -> CheckResult:
        params = criterion.params
        source_rel = params.get("source")
        pattern = params.get("pattern", "*")
        min_count = params.get("min_count")
        if source_rel is None or min_count is None:
            return self._skip(criterion, "missing source/min_count params")
        source = self._resolve_workspace_path(workspace, source_rel)
        if not source.exists():
            return self._skip(criterion, f"source missing: {source_rel}")
        if not source.is_dir():
            return self._skip(criterion, f"source not a directory: {source_rel}")
        matches = list(source.glob(pattern))
        if len(matches) >= int(min_count):
            return self._pass(
                criterion,
                f"{len(matches)} matches for {pattern} (>= {min_count})",
            )
        return self._fail(
            criterion,
            f"{len(matches)} matches for {pattern} (< {min_count})",
        )

    def _check_json_count_ge(
        self, criterion: Criterion, workspace: Path
    ) -> CheckResult:
        params = criterion.params
        source_rel = params.get("source")
        jsonpath = params.get("jsonpath", "")
        min_count = params.get("min_count")
        if not source_rel or min_count is None:
            return self._skip(criterion, "missing source/min_count params")
        data = self._load_json_artifact(workspace, source_rel)
        if data is None:
            return self._skip(criterion, f"json artifact missing: {source_rel}")
        values = _jsonpath_select(data, jsonpath)
        count = (
            len(values)
            if isinstance(values, list)
            else (1 if values is not None else 0)
        )
        if count >= int(min_count):
            return self._pass(criterion, f"count {count} >= {min_count}")
        return self._fail(criterion, f"count {count} < {min_count}")

    def _check_json_field_not_null(
        self, criterion: Criterion, workspace: Path
    ) -> CheckResult:
        params = criterion.params
        source_rel = params.get("source")
        jsonpath = params.get("jsonpath", "")
        forbid_values = set(params.get("forbid_values") or [])
        if not source_rel:
            return self._skip(criterion, "missing source param")
        data = self._load_json_artifact(workspace, source_rel)
        if data is None:
            return self._skip(criterion, f"json artifact missing: {source_rel}")
        values = _jsonpath_select(data, jsonpath)
        if not isinstance(values, list):
            values = [values]
        bad: list[Any] = []
        for v in values:
            if v is None or v in forbid_values:
                bad.append(v)
        if bad:
            return self._fail(criterion, f"null/forbidden values found: {bad[:3]}")
        if not values:
            return self._fail(criterion, f"no values found at {jsonpath}")
        return self._pass(criterion, f"all {len(values)} values non-null")

    def _check_json_field_equals(
        self, criterion: Criterion, workspace: Path
    ) -> CheckResult:
        params = criterion.params
        source_rel = params.get("source")
        jsonpath = params.get("jsonpath", "")
        expected = params.get("expected")
        if not source_rel:
            return self._skip(criterion, "missing source param")
        data = self._load_json_artifact(workspace, source_rel)
        if data is None:
            return self._skip(criterion, f"json artifact missing: {source_rel}")
        values = _jsonpath_select(data, jsonpath)
        if isinstance(values, list):
            if all(v == expected for v in values) and values:
                return self._pass(
                    criterion,
                    f"all {len(values)} values == {expected!r}",
                )
            return self._fail(criterion, f"some values != {expected!r}: {values}")
        if values == expected:
            return self._pass(criterion, f"value == {expected!r}")
        return self._fail(criterion, f"value {values!r} != expected {expected!r}")

    def _check_json_field_type(
        self, criterion: Criterion, workspace: Path
    ) -> CheckResult:
        params = criterion.params
        source_rel = params.get("source")
        jsonpath = params.get("jsonpath", "")
        expected_type = params.get("expected_type")
        type_map: dict[str, type | tuple[type, ...]] = {
            "float": (int, float),
            "int": int,
            "str": str,
            "bool": bool,
            "list": list,
            "dict": dict,
        }
        if not source_rel or expected_type not in type_map:
            return self._skip(criterion, "missing source or unsupported expected_type")
        data = self._load_json_artifact(workspace, source_rel)
        if data is None:
            return self._skip(criterion, f"json artifact missing: {source_rel}")
        values = _jsonpath_select(data, jsonpath)
        if not isinstance(values, list):
            values = [values]
        py_type = type_map[expected_type]
        bad = [v for v in values if not isinstance(v, py_type) or v is None]
        if bad:
            return self._fail(
                criterion,
                f"{len(bad)} values not of type {expected_type}",
            )
        if not values:
            return self._fail(criterion, f"no values at {jsonpath}")
        return self._pass(criterion, f"all {len(values)} values are {expected_type}")

    def _check_canary(self, criterion: Criterion, workspace: Path) -> CheckResult:
        """Confirm the workspace sentinel UUID appears in at least one
        other workspace file.

        When ``canary.txt`` is missing the check is *skipped* — callers
        opted out of canary injection. When it exists, every other file in
        the workspace is scanned (text only, binaries silently skipped) and
        the first file containing the UUID produces a pass.
        """
        canary_file = workspace / CANARY_FILENAME
        if not canary_file.is_file():
            return self._skip(criterion, f"{CANARY_FILENAME} not present")
        uuid = canary_file.read_text().strip()
        if not uuid:
            return self._skip(criterion, f"{CANARY_FILENAME} is empty")
        for child in sorted(workspace.rglob("*")):
            if not child.is_file() or child == canary_file:
                continue
            try:
                text = child.read_text(errors="replace")
            except OSError:
                continue
            if uuid in text:
                rel = child.relative_to(workspace)
                return self._pass(criterion, f"canary UUID found in {rel}")
        return self._fail(criterion, f"canary UUID {uuid!r} not found in workspace")

    # --------------------------------------------------------- workspace IO

    def _resolve_workspace_path(self, workspace: Path, source_rel: str) -> Path:
        """Resolve a ``source`` param relative to the workspace.

        Templated tokens like ``{repo}`` are substituted with the workspace
        path so criteria.toml paths remain portable between real runs and
        test fixtures.
        """
        substituted = source_rel.replace("{repo}", str(workspace))
        p = Path(substituted)
        if not p.is_absolute():
            p = workspace / p
        return p

    def _load_json_artifact(self, workspace: Path, source_rel: str) -> Any | None:
        path = self._resolve_workspace_path(workspace, source_rel)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None


# ---------------------------------------------------------------------------
# Tiny JSONPath-ish selector
# ---------------------------------------------------------------------------


def _jsonpath_select(data: Any, path: str) -> Any:
    """Resolve a minimal subset of JSONPath expressions.

    Supports:

    - ``$.field``
    - ``$.field.sub``
    - ``$.field[*].sub`` — flattens the list, collecting ``sub`` from each
      element.
    - ``$.jobs.*.runs-on`` — wildcard over dict values.

    Anything unsupported returns ``None`` so callers can treat it as a skip.
    """
    if not path:
        return data
    if path.startswith("$"):
        path = path[1:]
    path = path.lstrip(".")
    if not path:
        return data

    tokens = _tokenise_jsonpath(path)
    current: Any = data
    for token in tokens:
        if token == "[*]":
            if not isinstance(current, list):
                return None
            # Keep iterating: subsequent tokens apply to each item.
            current = list(current)
            continue
        if token == ".*":
            if isinstance(current, dict):
                current = list(current.values())
                continue
            return None
        # Field access on either a scalar/dict or a previously-flattened list.
        if isinstance(current, list):
            next_list: list[Any] = []
            for item in current:
                if isinstance(item, dict) and token in item:
                    next_list.append(item[token])
            current = next_list
        elif isinstance(current, dict):
            if token not in current:
                return None
            current = current[token]
        else:
            return None
    return current


def _tokenise_jsonpath(path: str) -> list[str]:
    """Split a JSONPath expression into field/wildcard tokens."""
    tokens: list[str] = []
    i = 0
    while i < len(path):
        ch = path[i]
        if ch == ".":
            i += 1
            if i < len(path) and path[i] == "*":
                tokens.append(".*")
                i += 1
            continue
        if ch == "[":
            end = path.find("]", i)
            if end == -1:
                break
            tokens.append(path[i : end + 1])
            i = end + 1
            continue
        # Read field name until the next . or [.
        j = i
        while j < len(path) and path[j] not in ".[":
            j += 1
        tokens.append(path[i:j])
        i = j
    return tokens


__all__ = [
    "CANARY_FILENAME",
    "CheckResult",
    "DEFAULT_COMMAND_TIMEOUT_S",
    "MIN_TIER_EVALUATED_PCT",
    "RESULT_FAIL",
    "RESULT_PASS",
    "RESULT_SKIP",
    "STATUS_EVALUATED",
    "STATUS_INCOMPLETE",
    "Verifier",
]
