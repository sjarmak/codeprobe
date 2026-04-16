"""Oracle-diff validation — supplementary quantitative signals for br7 evidence.

This module implements two of the three flavors defined in
`.beads/br7-validation-protocol.md`:

* **Flavor B — Score correlation:** rank correlation (Spearman + Kendall)
  between codeprobe's scores and an oracle's scores on a paired task set,
  plus the top-5 largest-gap outliers.
* **Flavor C — E2E pipeline divergence:** per-task pass/fail join between
  codeprobe's harness outputs and an oracle harness's outputs.

Flavor A (executable parity) was deliberately removed in the 2026-04-13
scope pivot on bead codeprobe-y67: for generative tasks, deterministic
test-based gates are vacuously gameable. The primary gate is Flavor R
(reviewer grading); the functions here produce supplementary numbers the
reviewer can consult.

ZFC: every function in this module performs deterministic arithmetic
(rank correlation, set joins, sorting by absolute gap). All semantic
judgment is delegated to the reviewer, who reads the artifacts written
here. No keyword matching, no quality scoring, no thresholds beyond the
caller-supplied numeric gates.
"""

from __future__ import annotations

import csv
import json
import math
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from scipy import stats

Flavor = Literal["B", "C"]
OracleCorpus = Literal["mcp-eval-tasks", "codescalebench", "enterprisebench"]
Status = Literal["pass", "fail"]

_OUTLIER_TOP_N = 5
_DEFAULT_MIN_N_FLAVOR_B = 5
_DEFAULT_MIN_MATCH_RATE_FLAVOR_C = 0.8

# Hard cap on JSON inputs to prevent runaway memory on malformed/adversarial
# files. The CSB MANIFEST.json is the largest legitimate input and is well
# under this size.
_MAX_JSON_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True)
class CheckOutcome:
    """A single named check inside a flavor result."""

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class FlavorResult:
    """Structured outcome of running one validation flavor.

    Mirrors the artifact `summary.json` written into ``artifact_dir`` so
    callers can either inspect the dataclass or read the JSON file.
    """

    status: Status
    flavor: Flavor
    oracle: OracleCorpus
    artifact_dir: Path
    checks: tuple[CheckOutcome, ...] = ()
    summary: dict[str, Any] = field(default_factory=dict)
    notes: str = ""


# ---------------------------------------------------------------------------
# Flavor B — score correlation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PairedRow:
    task_id: str
    codeprobe_score: float
    oracle_score: float
    oracle_source: str

    @property
    def gap(self) -> float:
        return abs(self.codeprobe_score - self.oracle_score)


def _safe_read_json(path: Path) -> Any:
    """Load JSON with a hard size cap so corrupt/adversarial files can't OOM."""
    size = path.stat().st_size
    if size > _MAX_JSON_BYTES:
        raise ValueError(
            f"JSON file {path} is {size} bytes, exceeds cap {_MAX_JSON_BYTES}"
        )
    return json.loads(path.read_text())


def _read_paired_csv(path: Path) -> list[_PairedRow]:
    rows: list[_PairedRow] = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        # row 1 is the header; data rows start at line 2
        for i, r in enumerate(reader, start=2):
            try:
                rows.append(
                    _PairedRow(
                        task_id=r["task_id"],
                        codeprobe_score=float(r["codeprobe_score"]),
                        oracle_score=float(r["oracle_score"]),
                        oracle_source=r.get("oracle_source", ""),
                    )
                )
            except (KeyError, ValueError) as exc:
                raise ValueError(f"{path}:row {i}: {exc}") from exc
    return rows


def _coerce(value: Any, fallback: float) -> float:
    """Coerce a possibly-NaN scipy result to float, falling back when invalid."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return fallback
    return fallback if math.isnan(f) else f


def _safe_correlation(
    cp: list[float], oracle: list[float]
) -> tuple[float, float, float, float]:
    """Return (spearman_r, spearman_p, kendall_r, kendall_p).

    scipy returns NaN and emits ConstantInputWarning for degenerate inputs
    (e.g. all identical values). Coerce NaN to 0.0 so downstream JSON
    serialization and threshold checks have well-defined semantics, and
    suppress the spurious warning.
    """
    if len(cp) < 2:
        return 0.0, 1.0, 0.0, 1.0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        sp = stats.spearmanr(cp, oracle)
        kt = stats.kendalltau(cp, oracle)
    return (
        _coerce(sp.statistic, 0.0),
        _coerce(sp.pvalue, 1.0),
        _coerce(kt.statistic, 0.0),
        _coerce(kt.pvalue, 1.0),
    )


def _write_outliers_md(path: Path, outliers: list[_PairedRow]) -> None:
    lines = ["# Outliers (top-5 by |codeprobe_score - oracle_score|)", ""]
    if not outliers:
        lines.append("_No outliers — paired scores were empty._")
    else:
        lines.append("| rank | task_id | codeprobe | oracle | gap | source |")
        lines.append("|------|---------|-----------|--------|-----|--------|")
        for i, r in enumerate(outliers, start=1):
            lines.append(
                f"| {i} | {r.task_id} | {r.codeprobe_score:.4f} | "
                f"{r.oracle_score:.4f} | {r.gap:.4f} | {r.oracle_source} |"
            )
    path.write_text("\n".join(lines) + "\n")


def flavor_b_score_correlation(
    *,
    paired_scores_csv: Path,
    min_correlation: float,
    artifact_dir: Path,
    min_n: int = _DEFAULT_MIN_N_FLAVOR_B,
    oracle: OracleCorpus = "codescalebench",
) -> FlavorResult:
    """Rank-correlation between codeprobe scores and oracle scores.

    Args:
        paired_scores_csv: CSV with columns
            ``task_id, codeprobe_score, oracle_score, oracle_source``.
        min_correlation: Spearman threshold; result is ``"pass"`` iff
            ``spearman >= min_correlation`` AND ``n_tasks >= min_n``.
        artifact_dir: Directory to write ``correlation.json``,
            ``outliers.md``, and ``summary.json`` into. Created if absent.
        min_n: Minimum sample size required to pass.
        oracle: Which oracle corpus the paired scores came from.

    Returns:
        :class:`FlavorResult` describing the outcome. Always writes
        artifacts even when the result is ``"fail"``.

    Raises:
        FileNotFoundError: if ``paired_scores_csv`` does not exist.
    """
    if not paired_scores_csv.exists():
        raise FileNotFoundError(f"paired scores CSV not found: {paired_scores_csv}")

    artifact_dir = artifact_dir.resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)

    rows = _read_paired_csv(paired_scores_csv)
    cp_scores = [r.codeprobe_score for r in rows]
    or_scores = [r.oracle_score for r in rows]
    n_tasks = len(rows)
    spearman, sp_p, kendall, kt_p = _safe_correlation(cp_scores, or_scores)

    checks: list[CheckOutcome] = []
    sample_ok = n_tasks >= min_n
    checks.append(
        CheckOutcome(
            name="sample_size",
            passed=sample_ok,
            detail=f"n={n_tasks}, required min_n={min_n}",
        )
    )
    correlation_ok = spearman >= min_correlation
    checks.append(
        CheckOutcome(
            name="correlation_threshold",
            passed=correlation_ok,
            detail=(
                f"spearman={spearman:.4f}, threshold={min_correlation:.4f}; "
                f"kendall={kendall:.4f}"
            ),
        )
    )

    status: Status = "pass" if (sample_ok and correlation_ok) else "fail"

    outliers = sorted(rows, key=lambda r: r.gap, reverse=True)[:_OUTLIER_TOP_N]

    correlation_payload = {
        "spearman": spearman,
        "kendall": kendall,
        "n_tasks": n_tasks,
        "p_values": {"spearman": sp_p, "kendall": kt_p},
    }
    (artifact_dir / "correlation.json").write_text(
        json.dumps(correlation_payload, indent=2) + "\n"
    )

    _write_outliers_md(artifact_dir / "outliers.md", outliers)

    summary_payload = {
        "status": status,
        "flavor": "B",
        "oracle": oracle,
        "n_tasks": n_tasks,
        "spearman": spearman,
        "kendall": kendall,
        "min_correlation": min_correlation,
        "min_n": min_n,
        "correlation_threshold_met": correlation_ok,
        "checks": [
            {"name": c.name, "passed": c.passed, "detail": c.detail} for c in checks
        ],
    }
    (artifact_dir / "summary.json").write_text(
        json.dumps(summary_payload, indent=2) + "\n"
    )

    return FlavorResult(
        status=status,
        flavor="B",
        oracle=oracle,
        artifact_dir=artifact_dir,
        checks=tuple(checks),
        summary=summary_payload,
    )


def flavor_b_from_csb_manifest(
    *,
    manifest_path: Path,
    codeprobe_scores: dict[str, float],
    output_csv: Path,
    run_filter: str,
) -> int:
    """Extract paired (codeprobe, oracle) score rows from a CSB manifest.

    The CSB run manifest at e.g.
    ``CodeScaleBench/runs/official/MANIFEST.json`` has shape
    ``{runs: {<run_key>: {tasks: {<task_id>: {status, reward, ...}}}}}``.

    For each ``task_id`` that appears in BOTH ``codeprobe_scores`` and
    the run identified by ``run_filter``, write one CSV row. Tasks that
    appear in only one side are silently excluded — Flavor B requires a
    paired set.

    Args:
        manifest_path: Path to the CSB ``MANIFEST.json``.
        codeprobe_scores: Mapping ``task_id -> codeprobe_score`` produced
            by the codeprobe scorer.
        output_csv: Destination for the paired CSV (parent dir created).
        run_filter: Exact run key to select from ``manifest['runs']``.

    Returns:
        Number of paired rows written.

    Raises:
        FileNotFoundError: if ``manifest_path`` does not exist.
        KeyError: if ``run_filter`` is not present in the manifest.
    """
    if not manifest_path.exists():
        raise FileNotFoundError(f"CSB manifest not found: {manifest_path}")

    manifest = _safe_read_json(manifest_path)
    runs = manifest.get("runs", {})
    if run_filter not in runs:
        available = sorted(runs.keys())
        sample = available[:5]
        suffix = "..." if len(available) > 5 else ""
        raise KeyError(
            f"run_filter '{run_filter}' not in manifest; "
            f"available: {sample}{suffix}"
        )
    run_tasks = runs[run_filter].get("tasks", {})

    output_csv = output_csv.resolve()
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    n_rows = 0
    with open(output_csv, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["task_id", "codeprobe_score", "oracle_score", "oracle_source"]
        )
        for task_id, cp_score in codeprobe_scores.items():
            entry = run_tasks.get(task_id)
            if entry is None:
                continue
            reward = entry.get("reward")
            if reward is None:
                continue
            writer.writerow([task_id, cp_score, reward, run_filter])
            n_rows += 1
    return n_rows


# ---------------------------------------------------------------------------
# Flavor C — E2E outcome divergence
# ---------------------------------------------------------------------------


def _read_outcomes(path: Path) -> dict[str, str]:
    """Load outcomes JSON in either dict or list-of-objects form."""
    raw = _safe_read_json(path)
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, list):
        out: dict[str, str] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                raise ValueError(
                    f"outcomes list entry must be an object: {entry!r}"
                )
            tid = entry.get("task_id")
            outcome = entry.get("outcome")
            if tid is None or outcome is None:
                raise ValueError(
                    f"outcomes list entry missing task_id/outcome: {entry!r}"
                )
            out[str(tid)] = str(outcome)
        return out
    raise ValueError(
        f"outcomes JSON at {path} must be a dict or list of objects, "
        f"got {type(raw).__name__}"
    )


def flavor_c_e2e_divergence(
    *,
    codeprobe_outcomes_json: Path,
    oracle_outcomes_json: Path,
    artifact_dir: Path,
    min_match_rate: float = _DEFAULT_MIN_MATCH_RATE_FLAVOR_C,
    oracle: OracleCorpus = "codescalebench",
) -> FlavorResult:
    """Join per-task outcomes from codeprobe vs an oracle harness.

    Both inputs are JSON, accepted in two shapes:

    * ``{"task_id": "passed"|"failed"|...}``
    * ``[{"task_id": ..., "outcome": ...}, ...]``

    The joined set is the intersection of task ids. Outcome equality is
    a strict string compare — callers should normalize outcomes (e.g.
    ``"passed"`` vs ``"PASS"``) before invoking this function if needed.

    Status is ``"fail"`` when there is no overlap, when the join is
    empty, or when ``match_rate < min_match_rate``.

    Args:
        codeprobe_outcomes_json: Per-task outcomes from codeprobe.
        oracle_outcomes_json: Per-task outcomes from the oracle harness.
        artifact_dir: Directory for ``e2e_outcomes.csv`` + ``summary.json``.
        min_match_rate: Minimum match rate required to pass.
        oracle: Which oracle corpus produced the comparison outcomes.

    Returns:
        :class:`FlavorResult` describing the outcome.

    Raises:
        FileNotFoundError: if either input JSON does not exist.
    """
    if not codeprobe_outcomes_json.exists():
        raise FileNotFoundError(
            f"codeprobe outcomes JSON not found: {codeprobe_outcomes_json}"
        )
    if not oracle_outcomes_json.exists():
        raise FileNotFoundError(
            f"oracle outcomes JSON not found: {oracle_outcomes_json}"
        )

    artifact_dir = artifact_dir.resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)

    cp = _read_outcomes(codeprobe_outcomes_json)
    oracle_outcomes = _read_outcomes(oracle_outcomes_json)

    cp_ids = set(cp.keys())
    or_ids = set(oracle_outcomes.keys())
    joined_ids = sorted(cp_ids & or_ids)
    only_cp = sorted(cp_ids - or_ids)
    only_or = sorted(or_ids - cp_ids)

    n_tasks = len(joined_ids)
    n_match = sum(1 for t in joined_ids if cp[t] == oracle_outcomes[t])
    match_rate = (n_match / n_tasks) if n_tasks > 0 else 0.0

    csv_path = artifact_dir / "e2e_outcomes.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["task_id", "codeprobe_outcome", "oracle_outcome", "match"]
        )
        for tid in joined_ids:
            cp_o = cp[tid]
            or_o = oracle_outcomes[tid]
            matched = cp_o == or_o
            writer.writerow([tid, cp_o, or_o, "true" if matched else "false"])

    checks: list[CheckOutcome] = []
    if n_tasks == 0:
        checks.append(
            CheckOutcome(
                name="overlap",
                passed=False,
                detail=(
                    "no overlap between codeprobe and oracle task ids "
                    f"(disjoint sets: |cp|={len(cp_ids)}, |oracle|={len(or_ids)})"
                ),
            )
        )
    else:
        checks.append(
            CheckOutcome(
                name="overlap",
                passed=True,
                detail=f"n_joined={n_tasks}",
            )
        )
        checks.append(
            CheckOutcome(
                name="match_rate",
                passed=match_rate >= min_match_rate,
                detail=(
                    f"match_rate={match_rate:.4f}, "
                    f"threshold={min_match_rate:.4f}"
                ),
            )
        )

    status: Status = "pass" if all(c.passed for c in checks) and n_tasks > 0 else "fail"

    summary_payload = {
        "status": status,
        "flavor": "C",
        "oracle": oracle,
        "n_tasks": n_tasks,
        "n_match": n_match,
        "match_rate": match_rate,
        "min_match_rate": min_match_rate,
        "n_only_codeprobe": len(only_cp),
        "n_only_oracle": len(only_or),
        "only_codeprobe_sample": only_cp[:10],
        "only_oracle_sample": only_or[:10],
        "checks": [
            {"name": c.name, "passed": c.passed, "detail": c.detail} for c in checks
        ],
    }
    (artifact_dir / "summary.json").write_text(
        json.dumps(summary_payload, indent=2) + "\n"
    )

    return FlavorResult(
        status=status,
        flavor="C",
        oracle=oracle,
        artifact_dir=artifact_dir,
        checks=tuple(checks),
        summary=summary_payload,
    )


__all__ = [
    "CheckOutcome",
    "Flavor",
    "FlavorResult",
    "OracleCorpus",
    "flavor_b_from_csb_manifest",
    "flavor_b_score_correlation",
    "flavor_c_e2e_divergence",
]
