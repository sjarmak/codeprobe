"""Acceptance criteria verifier.

Evaluates a list of :class:`~acceptance.loader.Criterion` objects against a
Test Agent workspace directory and produces a ``verdict.json`` summary.

The verifier implements a three-tier evaluation model:

- **structural** — Python introspection and source-file inspection. These
  checks run without a workspace and are effectively instant. The two
  import-based check types (``import_equals``, ``dataclass_has_fields``)
  import in the verifier's own interpreter by default; passing
  ``Verifier(..., python_interpreter=...)`` makes them introspect via a
  subprocess in that interpreter instead — see :class:`Verifier`.
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

import dataclasses
import importlib
import json
import re
import subprocess
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

#: Python source executed via ``<interpreter> -c <script> <module> <symbol>``
#: to introspect a module/symbol inside a *different* interpreter than the
#: one running the verifier (see ``Verifier.python_interpreter``). Emits a
#: single JSON line to stdout and always exits 0 for handled outcomes
#: (import error, missing symbol) so the parent can tell "expected
#: introspection outcome" apart from a genuine subprocess crash.
_SUBPROCESS_INTROSPECT_SCRIPT: str = """
import dataclasses
import importlib
import json
import sys

module_name, symbol = sys.argv[1], sys.argv[2]
try:
    module = importlib.import_module(module_name)
except ImportError as exc:
    print(json.dumps({"status": "import_error", "detail": str(exc)}))
    sys.exit(0)
if not hasattr(module, symbol):
    print(json.dumps({"status": "missing_symbol"}))
    sys.exit(0)
obj = getattr(module, symbol)
try:
    field_names = sorted(f.name for f in dataclasses.fields(obj))
except TypeError:
    field_names = sorted(getattr(obj, "__annotations__", {}) or {})
try:
    json.dumps(obj)
    value, value_serializable = obj, True
except TypeError:
    value, value_serializable = repr(obj), False
print(json.dumps({
    "status": "ok",
    "fields": field_names,
    "value": value,
    "value_serializable": value_serializable,
}))
"""


def _canonicalize_for_import_equals(value: Any) -> Any:
    """Coerce ``value`` to the shape it would have after a JSON round-trip.

    Subprocess-mode ``import_equals`` (see :meth:`Verifier._introspect_via_subprocess`)
    necessarily serializes the introspected value through ``json.dumps`` /
    ``json.loads`` to cross the process boundary — this turns tuples into
    lists and stringifies non-string dict keys. Without this normalization,
    in-process mode compares the live Python object directly, so the same
    ``import_equals`` criterion could pass or fail depending solely on
    whether ``python_interpreter`` happens to be set (e.g. a tuple-valued
    constant checked against a TOML array). Applying the same coercion here
    keeps the two modes semantically equivalent, matching what the module
    docstring promises.
    """
    if isinstance(value, (tuple, list)):
        return [_canonicalize_for_import_equals(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _canonicalize_for_import_equals(v) for k, v in value.items()}
    return value


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
    skip_reason: str | None = None  # "eval_mode" when skipped due to mode mismatch


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
        python_interpreter: Optional path to a Python interpreter (e.g. a
            staged release venv's ``bin/python``). When set, the
            ``import_equals`` and ``dataclass_has_fields`` structural
            handlers introspect the target module/symbol by running a
            subprocess in *that* interpreter instead of importing in the
            verifier's own (caller) interpreter — so the check reflects
            what's actually installed in the staged environment. When
            ``None`` (the default), behavior is unchanged: imports happen
            in-process, in the caller's interpreter.
    """

    def __init__(
        self,
        criteria_path: Path | str,
        project_root: Path | str | None = None,
        eval_mode: str | None = None,
        python_interpreter: Path | str | None = None,
    ) -> None:
        self.criteria_path = Path(criteria_path).resolve()
        self.criteria: list[Criterion] = load_criteria(self.criteria_path)
        self.eval_mode: str | None = eval_mode
        self.python_interpreter: Path | None = (
            Path(python_interpreter) if python_interpreter is not None else None
        )
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
            mode_skip = self._check_eval_mode(criterion)
            if mode_skip is not None:
                results.append(mode_skip)
                continue
            handler = self._handlers().get(criterion.check_type)
            if handler is None:
                results.append(
                    CheckResult(
                        criterion_id=criterion.id,
                        tier=criterion.tier,
                        severity=criterion.severity,
                        result=RESULT_SKIP,
                        evidence=(f"unsupported check_type: {criterion.check_type!r}"),
                        skip_reason="no_handler",
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
        mode_skip_count = 0
        no_handler_count = 0
        failures: list[dict[str, str]] = []
        # Criteria with no registered Verifier handler: structurally
        # unevaluable in ANY eval mode (not just the current one), unlike
        # eval_mode skips which are mode-specific. Reported distinctly so a
        # caller can tell "this criterion is dry-run-only" apart from "this
        # criterion can never be checked until a handler is written" — see
        # the acceptance-loop doctrine's tier-padding discussion.
        no_handler_criteria: list[dict[str, str]] = []
        # Track eval_mode + no_handler skips per tier so they don't penalize
        # evaluated_pct (both are structurally unevaluable, not missing
        # coverage from a bad run).
        tier_mode_skips: dict[str, int] = {tier: 0 for tier in sorted(ALLOWED_TIERS)}

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
                if res.skip_reason == "eval_mode":
                    mode_skip_count += 1
                    tier_mode_skips[res.tier] += 1
                elif res.skip_reason == "no_handler":
                    no_handler_count += 1
                    tier_mode_skips[res.tier] += 1
                    no_handler_criteria.append(
                        {
                            "criterion_id": res.criterion_id,
                            "tier": res.tier,
                            "severity": res.severity,
                        }
                    )

        evaluated_pct: dict[str, float] = {}
        for tier, counts in tier_counts.items():
            # Exclude eval_mode skips from the denominator — they are
            # structurally unevaluable in this mode, not missing coverage.
            effective_total = counts["total"] - tier_mode_skips[tier]
            if effective_total <= 0:
                evaluated_pct[tier] = 100.0 if counts["total"] > 0 else 0.0
                continue
            evaluated = counts["pass"] + counts["fail"]
            evaluated_pct[tier] = round(evaluated / effective_total * 100.0, 2)

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
            "eval_mode": self.eval_mode,
            "pass_count": pass_count,
            "fail_count": fail_count,
            "skip_count": skip_count,
            "mode_skip_count": mode_skip_count,
            "no_handler_count": no_handler_count,
            "no_handler_criteria": no_handler_criteria,
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

    def _check_eval_mode(self, criterion: Criterion) -> CheckResult | None:
        """Return a skip result if the criterion requires an eval mode that
        does not match the current mode. Returns ``None`` when the criterion
        should proceed to its normal check handler.

        When ``eval_mode`` is ``None`` (default), criteria with
        ``eval_mode_required`` are skipped because the verifier is running
        outside a full eval context.
        """
        required = criterion.eval_mode_required
        if required is None:
            return None
        if self.eval_mode == required:
            return None
        current = self.eval_mode or "none"
        result = self._skip(
            criterion,
            f"eval_mode mismatch: requires {required!r}, current is {current!r}",
        )
        return dataclasses.replace(result, skip_reason="eval_mode")

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

        if self.python_interpreter is not None:
            early, payload = self._introspect_via_subprocess(
                criterion, module_name, symbol
            )
            if early is not None:
                return early
            assert payload is not None
            if not payload["value_serializable"]:
                return self._fail(
                    criterion,
                    f"{module_name}.{symbol} value is not JSON-serializable "
                    "for staged-interpreter introspection",
                )
            actual = payload["value"]
            if actual == expected:
                return self._pass(
                    criterion,
                    f"{module_name}.{symbol} == {expected!r} (staged interpreter)",
                )
            return self._fail(
                criterion,
                f"{module_name}.{symbol} == {actual!r}, expected {expected!r} "
                "(staged interpreter)",
            )

        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            return self._skip(criterion, f"cannot import {module_name}: {exc}")
        if not hasattr(module, symbol):
            return self._fail(criterion, f"{module_name}.{symbol} not defined")
        actual = getattr(module, symbol)
        if _canonicalize_for_import_equals(actual) == expected:
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

        if self.python_interpreter is not None:
            early, payload = self._introspect_via_subprocess(
                criterion, module_name, symbol
            )
            if early is not None:
                return early
            assert payload is not None
            present = set(payload["fields"])
            missing = [f for f in required if f not in present]
            if missing:
                return self._fail(
                    criterion,
                    f"{module_name}.{symbol} missing fields: {missing} "
                    "(staged interpreter)",
                )
            return self._pass(
                criterion,
                f"{module_name}.{symbol} has all required fields (staged interpreter)",
            )

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

    def _introspect_via_subprocess(
        self, criterion: Criterion, module_name: str, symbol: str
    ) -> tuple[CheckResult | None, dict[str, Any] | None]:
        """Introspect ``module_name.symbol`` inside :attr:`python_interpreter`.

        Returns ``(result, None)`` when the subprocess path itself resolves
        the criterion (crash, timeout, unparseable output, import error, or
        missing symbol all map to a terminal skip/fail here) — the caller
        should return ``result`` directly. Returns ``(None, payload)`` with
        the parsed ``status: "ok"`` payload when the caller should continue
        with its own pass/fail comparison.

        Runs with ``-I`` (isolated mode) so the child genuinely resolves
        ``module_name`` against :attr:`python_interpreter`'s own venv
        site-packages: without it, the child inherits the caller's
        ``PYTHONPATH`` (which is prepended ahead of venv site-packages) and
        ``-c`` prepends the caller's cwd to ``sys.path``, so a module on
        either could shadow the staged venv's install and produce a false
        pass. ``-I`` does not affect resolution of the venv's own
        site-packages, which lives under the interpreter's prefix.
        """
        assert self.python_interpreter is not None
        cmd = [
            str(self.python_interpreter),
            "-I",  # isolated mode: ignore PYTHONPATH/cwd/user site so this
            # genuinely probes python_interpreter's own venv site-packages,
            # not whatever the caller's environment happens to leak in.
            "-c",
            _SUBPROCESS_INTROSPECT_SCRIPT,
            module_name,
            symbol,
        ]
        try:
            completed = subprocess.run(  # noqa: S603 - fixed script, trusted args
                cmd,
                capture_output=True,
                text=True,
                timeout=DEFAULT_COMMAND_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return (
                self._skip(
                    criterion,
                    "staged-interpreter introspection timed out after "
                    f"{DEFAULT_COMMAND_TIMEOUT_S}s",
                ),
                None,
            )
        except OSError as exc:
            return (
                self._skip(
                    criterion,
                    f"staged-interpreter introspection failed to start: {exc}",
                ),
                None,
            )
        if completed.returncode != 0:
            return (
                self._skip(
                    criterion,
                    f"staged-interpreter introspection exited "
                    f"{completed.returncode}: {completed.stderr.strip()[:500]}",
                ),
                None,
            )
        try:
            payload = json.loads(completed.stdout.strip() or "{}")
        except json.JSONDecodeError:
            return (
                self._skip(
                    criterion,
                    "staged-interpreter introspection emitted unparseable "
                    f"output: {completed.stdout[:200]!r}",
                ),
                None,
            )
        if not isinstance(payload, dict):
            return (
                self._skip(
                    criterion,
                    "staged-interpreter introspection emitted a non-object "
                    "JSON payload",
                ),
                None,
            )
        status = payload.get("status")
        if status == "import_error":
            return (
                self._skip(
                    criterion,
                    f"cannot import {module_name} in staged interpreter: "
                    f"{payload.get('detail')}",
                ),
                None,
            )
        if status == "missing_symbol":
            return (
                self._fail(
                    criterion,
                    f"{module_name}.{symbol} not defined (staged interpreter)",
                ),
                None,
            )
        if status != "ok":
            return (
                self._skip(
                    criterion,
                    f"staged-interpreter introspection returned unexpected "
                    f"status: {status!r}",
                ),
                None,
            )
        return None, payload

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
