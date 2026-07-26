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
import tempfile
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

#: Registry mapping ``(module, symbol)`` of the dataclass a
#: ``dataclass_roundtrip`` criterion names to the production function that
#: actually deserializes it from disk. R16 (the contract this check_type
#: enforces) is "no KNOWN dataclass field is lost or altered on load" — that
#: can only be a real assertion if the check calls the SAME code the
#: production loader calls, not a reimplementation of its field-filter
#: logic (a reimplementation of a lossless operation can never observe the
#: loss it exists to catch). Each entry takes ``(exp_dir, config_label)``
#: exactly like ``core.experiment.load_config_results`` and returns an
#: object exposing ``.completed`` (a sequence of dataclass instances).
#: Deliberately explicit and fail-loud rather than falling back to a
#: reimplementation for an unregistered pair — see
#: ``Verifier._check_dataclass_roundtrip``.
_DATACLASS_ROUNDTRIP_LOADERS: dict[tuple[str, str], tuple[str, str]] = {
    ("codeprobe.models.experiment", "CompletedTask"): (
        "codeprobe.core.experiment",
        "load_config_results",
    ),
}

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
            "dataclass_roundtrip": Verifier._check_dataclass_roundtrip,
            "regex_present": Verifier._check_regex_present,
            "regex_absent": Verifier._check_regex_absent,
            "pyproject_deps_bounded": Verifier._check_pyproject_deps_bounded,
            "yaml_field_equal": Verifier._check_yaml_field_equal,
            # Behavioral (CLI outputs from workspace).
            "cli_exit_code": Verifier._check_cli_exit_code,
            "cli_help_contains": Verifier._check_cli_help_contains,
            "cli_stdout_contains": Verifier._check_cli_stdout_contains,
            "cli_writes_file": Verifier._check_cli_writes_file,
            "file_exists": Verifier._check_file_exists,
            "stdout_contains": Verifier._check_stdout_contains,
            "stderr_contains": Verifier._check_stderr_contains,
            "stream_separation": Verifier._check_stream_separation,
            "json_lines_valid": Verifier._check_json_lines_valid,
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

    @staticmethod
    def _extract_records(data: Any) -> list[Any] | None:
        """Pull the list of records to round-trip out of a loaded fixture.

        Accepts a bare top-level list, or a dict whose ``completed_tasks``
        (the aggregated ``.codeprobe/results.json`` shape) or ``completed``
        (the per-arm ``runs/<arm>/results.json`` shape written by
        ``core.experiment.save_config_results``) key holds the list. Returns
        ``None`` when no record list can be located so the caller can fail
        loudly rather than silently round-trip zero records.
        """
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("completed_tasks", "completed"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
        return None

    def _check_dataclass_roundtrip(
        self, criterion: Criterion, _workspace: Path
    ) -> CheckResult:
        """Round-trip every record in a JSON fixture through the REAL
        production loader for ``module.symbol``.

        R16 is "no KNOWN dataclass field is lost or altered when results.json
        is loaded back". Loads ``fixture`` (project-relative), extracts its
        record list, writes it to a throwaway ``exp_dir/runs/<label>/
        results.json`` in the on-disk shape ``core.experiment.
        save_config_results`` produces, then calls the SAME function
        production calls to read it back — the loader registered for
        ``(module, symbol)`` in ``_DATACLASS_ROUNDTRIP_LOADERS`` — rather
        than reimplementing its field-filter logic inline. A field-drop
        regression in the real loader therefore actually fails this
        criterion; a hand-rolled reimplementation of a lossless operation
        never could. Unknown keys (newer-schema fields, e.g. a fixture
        record's deliberate ``provenance_note``) are legitimately dropped —
        that is not data loss, so they are excluded from the comparison.

        If ``(module, symbol)`` has no registered loader, this fails loudly
        (structural, no I/O flakiness) naming the gap rather than silently
        falling back to a construct-and-asdict reimplementation whose fail
        path can never fire — see the module-level registry's docstring.
        """
        params = criterion.params
        module_name = params.get("module")
        symbol = params.get("symbol")
        fixture_rel = params.get("fixture")
        if not module_name or not symbol or not fixture_rel:
            return self._skip(criterion, "missing module/symbol/fixture params")
        fixture_path = self._resolve_project_file(fixture_rel)
        if not fixture_path.is_file():
            return self._skip(criterion, f"fixture not found: {fixture_rel}")
        try:
            data = json.loads(fixture_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            return self._skip(criterion, f"fixture unreadable: {fixture_rel}: {exc}")
        records = self._extract_records(data)
        if records is None:
            return self._fail(
                criterion,
                f"fixture {fixture_rel} has no record list "
                "(expected a top-level list, or a 'completed_tasks'/'completed' key)",
            )
        if not records:
            return self._fail(
                criterion, f"fixture {fixture_rel} contains zero records to round-trip"
            )
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            return self._skip(criterion, f"cannot import {module_name}: {exc}")
        cls = getattr(module, symbol, None)
        if cls is None:
            return self._fail(criterion, f"{module_name}.{symbol} not defined")
        try:
            field_names = {f.name for f in dataclasses.fields(cls)}
        except TypeError:
            return self._fail(criterion, f"{module_name}.{symbol} is not a dataclass")

        loader_ref = _DATACLASS_ROUNDTRIP_LOADERS.get((module_name, symbol))
        if loader_ref is None:
            return self._fail(
                criterion,
                f"no production loader registered for {module_name}.{symbol} "
                "in acceptance.verify._DATACLASS_ROUNDTRIP_LOADERS — a "
                "construct-and-asdict reimplementation cannot observe a real "
                "field-drop regression (R16's stated purpose), so this check "
                "refuses to fall back to one; register the real on-disk "
                "loader for this dataclass before adding this criterion",
            )
        loader_module_name, loader_symbol = loader_ref
        try:
            loader_module = importlib.import_module(loader_module_name)
        except ImportError as exc:
            return self._skip(
                criterion, f"cannot import loader {loader_module_name}: {exc}"
            )
        loader = getattr(loader_module, loader_symbol, None)
        if loader is None:
            return self._fail(
                criterion,
                f"registered loader {loader_module_name}.{loader_symbol} not defined",
            )

        config_label = "acceptance-roundtrip-check"
        for idx, rec in enumerate(records):
            if not isinstance(rec, dict):
                return self._fail(criterion, f"record {idx} is not a JSON object")
            label = rec.get("task_id", idx)
            known = {k: v for k, v in rec.items() if k in field_names}

            # One record per loader call (not a single batch call over all
            # records) so a construction failure is attributed to the
            # record that caused it, exactly as the prior reimplementation
            # did — while still calling the real production function.
            with tempfile.TemporaryDirectory() as tmp:
                exp_dir = Path(tmp)
                runs_dir = exp_dir / "runs" / config_label
                runs_dir.mkdir(parents=True)
                (runs_dir / "results.json").write_text(
                    json.dumps({"config": config_label, "completed": [rec]})
                )
                try:
                    loaded = loader(exp_dir, config_label)
                except Exception as exc:  # noqa: BLE001 - any loader failure is evidence
                    return self._fail(
                        criterion,
                        f"record {label!r} cannot construct {symbol} via "
                        f"the production loader {loader_module_name}."
                        f"{loader_symbol}: {exc}",
                    )

            loaded_completed = getattr(loaded, "completed", None)
            if not loaded_completed:
                return self._fail(
                    criterion,
                    f"record {label!r} was dropped entirely by the "
                    f"production loader {loader_module_name}.{loader_symbol}",
                )
            obj = loaded_completed[0]
            roundtripped = dataclasses.asdict(obj)
            lost = [k for k, v in known.items() if roundtripped.get(k) != v]
            if lost:
                return self._fail(
                    criterion,
                    f"record {label!r} lost/altered fields through the "
                    f"production loader {loader_module_name}."
                    f"{loader_symbol}: {lost}",
                )
        return self._pass(
            criterion,
            f"all {len(records)} records round-trip through "
            f"{loader_module_name}.{loader_symbol} ({symbol}) without "
            "known-field loss",
        )

    def _check_yaml_field_equal(
        self, criterion: Criterion, _workspace: Path
    ) -> CheckResult:
        """Assert a JSONPath-selected field is (un)equal across YAML files.

        Parses each ``files`` entry with PyYAML, selects values via the same
        minimal JSONPath the JSON handlers use (``$.jobs.*.runs-on`` etc.),
        and — when ``must_match`` is truthy — requires every collected value
        to be equal. A missing file skips (unevaluable); an empty selection
        fails (the field the contract names is absent everywhere); and an
        entry the wildcard step matches but that lacks the selected leaf
        (e.g. a reusable-workflow ``uses:`` job with no ``runs-on``) also
        fails rather than being silently dropped from the comparison —
        partial absence must be observable, never read as "every value that
        exists happens to match".
        """
        import yaml

        params = criterion.params
        files = params.get("files")
        jsonpath = params.get("jsonpath", "")
        must_match = bool(params.get("must_match", True))
        if not isinstance(files, list) or not files:
            return self._skip(criterion, "missing/invalid files param")
        collected: list[Any] = []
        missing_by_file: dict[str, list[str]] = {}
        for rel in files:
            if not isinstance(rel, str):
                return self._skip(criterion, f"non-string file entry: {rel!r}")
            fp = self._resolve_project_file(rel)
            if not fp.is_file():
                return self._skip(criterion, f"file not found: {rel}")
            try:
                doc = yaml.safe_load(fp.read_text())
            except yaml.YAMLError as exc:
                return self._skip(criterion, f"yaml parse error in {rel}: {exc}")
            selected, gaps = _jsonpath_select_reporting_gaps(doc, jsonpath)
            collected.extend(selected)
            if gaps:
                missing_by_file[rel] = gaps
        if missing_by_file:
            return self._fail(
                criterion,
                f"{jsonpath!r} matched but the field was absent for: "
                f"{missing_by_file} (a matched entry lacking the selected "
                "field is not silently counted as equal)",
            )
        if not collected:
            return self._fail(
                criterion, f"no values selected by {jsonpath!r} across {files}"
            )
        all_equal = all(v == collected[0] for v in collected)
        if must_match:
            if all_equal:
                return self._pass(
                    criterion,
                    f"all {len(collected)} values equal ({collected[0]!r})",
                )
            distinct = sorted({repr(v) for v in collected})
            return self._fail(criterion, f"values differ across {files}: {distinct}")
        if not all_equal:
            return self._pass(criterion, f"{len(collected)} values are not all equal")
        return self._fail(
            criterion,
            f"all {len(collected)} values equal but must_match=false required difference",
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

    def _exit_gate(
        self, criterion: Criterion, workspace: Path
    ) -> CheckResult | None:
        """Return a failing result unless the captured command exited exactly 0.

        ``stream_separation`` and ``json_lines_valid`` assert a property of a
        *successful* command's output. Called only after the command's output
        artifact is confirmed present, so the command DID run and produced
        output. A missing, empty, malformed, or nonzero ``<id>.exit`` therefore
        cannot be an honest pass — none of them let us confirm the command
        succeeded, and a JSON error envelope (or log lines written before a
        failure) would otherwise green vacuously. Returns ``None`` only for an
        exit code of exactly 0.
        """
        exit_file = workspace / f"{criterion.id}.exit"
        if not exit_file.is_file():
            return self._fail(
                criterion,
                "exit artifact missing; the command produced output but its "
                "exit status is unknown, so success cannot be confirmed",
            )
        raw = exit_file.read_text(errors="replace").strip()
        if not raw:
            return self._fail(
                criterion,
                "exit artifact is empty; the command's exit status is unknown, "
                "so success cannot be confirmed",
            )
        try:
            code = int(raw)
        except ValueError:
            return self._fail(
                criterion, f"exit artifact is not an integer: {raw!r}"
            )
        if code != 0:
            return self._fail(
                criterion,
                f"command exited {code} (expected 0); a nonzero exit means the "
                "command errored, so its output is not evidence of success",
            )
        return None

    def _check_stream_separation(
        self, criterion: Criterion, workspace: Path
    ) -> CheckResult:
        """Verify a command kept stdout and stderr honestly separated.

        Reads the captured ``<id>.stdout`` artifact (missing → skip). Two
        param shapes are supported, read generically:

        - ``stdout_must_parse_as = "json"``: stdout must parse as a single
          JSON document (warnings/logs belong on stderr, so any leaked log
          line breaks the parse).
        - ``stdout_must_not_contain = <s>``: the substring must be absent
          from stdout (e.g. an ``INFO codeprobe`` log line that belongs on
          stderr). A companion ``stderr_may_contain`` is permissive by
          definition and asserts nothing.
        """
        params = criterion.params
        stdout_file = workspace / f"{criterion.id}.stdout"
        if not stdout_file.is_file():
            return self._skip(criterion, f"stdout artifact missing: {stdout_file.name}")
        stdout_text = stdout_file.read_text(errors="replace")

        exit_fail = self._exit_gate(criterion, workspace)
        if exit_fail is not None:
            return exit_fail

        parse_as = params.get("stdout_must_parse_as")
        if parse_as == "json":
            stripped = stdout_text.strip()
            if not stripped:
                return self._fail(criterion, "stdout is empty; expected a JSON document")
            try:
                json.loads(stripped)
            except json.JSONDecodeError as exc:
                return self._fail(
                    criterion,
                    f"stdout is not pure JSON ({exc}); leading bytes: {stripped[:120]!r}",
                )
            return self._pass(
                criterion, "stdout parses as JSON (no warnings leaked onto stdout)"
            )

        must_not_contain = params.get("stdout_must_not_contain")
        if must_not_contain is not None:
            if str(must_not_contain) in stdout_text:
                return self._fail(
                    criterion,
                    f"stdout contains {must_not_contain!r}; it belongs on stderr only",
                )
            return self._pass(criterion, f"{must_not_contain!r} absent from stdout")

        return self._skip(
            criterion,
            "no stdout_must_parse_as or stdout_must_not_contain param",
        )

    def _check_json_lines_valid(
        self, criterion: Criterion, workspace: Path
    ) -> CheckResult:
        """Every non-empty line on a channel must be a JSON object with keys.

        Reads ``<id>.<channel>`` (``channel`` param, default ``stdout``;
        missing artifact → skip). Each non-blank line must ``json.loads`` to
        an object containing every entry of ``required_keys``. An artifact
        that exists but holds no non-blank lines fails (the ``--log-format
        json`` contract promised structured lines and emitted none).
        """
        params = criterion.params
        channel = params.get("channel", "stdout")
        required_keys = params.get("required_keys") or []
        if channel not in ("stdout", "stderr"):
            return self._skip(criterion, f"unsupported channel: {channel!r}")
        artifact = workspace / f"{criterion.id}.{channel}"
        if not artifact.is_file():
            return self._skip(criterion, f"{channel} artifact missing: {artifact.name}")
        exit_fail = self._exit_gate(criterion, workspace)
        if exit_fail is not None:
            return exit_fail
        lines = [ln for ln in artifact.read_text(errors="replace").splitlines() if ln.strip()]
        if not lines:
            return self._fail(
                criterion, f"no non-empty lines on {channel}; expected JSON log lines"
            )
        for idx, line in enumerate(lines):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                return self._fail(
                    criterion,
                    f"{channel} line {idx} is not valid JSON ({exc}): {line[:120]!r}",
                )
            if not isinstance(obj, dict):
                return self._fail(
                    criterion, f"{channel} line {idx} is a JSON {type(obj).__name__}, not an object"
                )
            missing = [k for k in required_keys if k not in obj]
            if missing:
                return self._fail(
                    criterion, f"{channel} line {idx} missing required keys: {missing}"
                )
        return self._pass(
            criterion,
            f"all {len(lines)} {channel} lines are JSON objects with {list(required_keys)}",
        )

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


def _jsonpath_select_reporting_gaps(data: Any, path: str) -> tuple[list[Any], list[str]]:
    """Resolve a single dict-wildcard JSONPath, surfacing partial absence.

    Handles the ``$.<key1>...<keyN>.*.<leaf1>...<leafM>`` shape — exactly
    what :meth:`Verifier._check_yaml_field_equal` uses (``$.jobs.*.runs-on``
    etc.) — and returns ``(values, missing_keys)`` where ``missing_keys``
    names every dict key produced by the ``.*`` wildcard step whose value
    did NOT contain the subsequent leaf path.

    :func:`_jsonpath_select` silently drops such entries from its result
    (a job dict without ``runs-on`` simply never reaches the collected
    list), which makes a job that legitimately lacks the selected field
    indistinguishable from a job that was never selected. This function
    exists to make that distinction observable so callers can fail loudly
    instead of reporting an accidental "match" over a shrunken set.

    Any shape this function does not specifically handle (no ``.*`` token,
    more than one ``.*`` token, or a ``[*]`` list-wildcard) falls back to
    :func:`_jsonpath_select` with no gap tracking, preserving its existing
    behaviour for every other current caller.
    """
    stripped = path[1:] if path.startswith("$") else path
    stripped = stripped.lstrip(".")
    tokens = _tokenise_jsonpath(stripped) if stripped else []

    if tokens.count(".*") != 1 or "[*]" in tokens:
        selected = _jsonpath_select(data, path)
        if isinstance(selected, list):
            return (selected, [])
        return (([selected] if selected is not None else []), [])

    wildcard_idx = tokens.index(".*")
    pre_tokens = tokens[:wildcard_idx]
    post_tokens = tokens[wildcard_idx + 1 :]

    current: Any = data
    for token in pre_tokens:
        if not isinstance(current, dict) or token not in current:
            return ([], [])
        current = current[token]
    if not isinstance(current, dict):
        return ([], [])

    values: list[Any] = []
    missing: list[str] = []
    for key, item in current.items():
        node = item
        found = True
        for token in post_tokens:
            if isinstance(node, dict) and token in node:
                node = node[token]
            else:
                found = False
                break
        if found:
            values.append(node)
        else:
            missing.append(key)
    return (values, missing)


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
