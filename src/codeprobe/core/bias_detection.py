"""Aggregate-time bias detection — surface measurement artifacts.

When a config's MCP tool surface includes the same backend that produced
the ground truth, comparisons against a baseline that lacks that backend
become tautological: the with-MCP config recovers the GT because it
called the grading rubric, not because the tool added value.

These are *honest signal* warnings, not score adjustments. Detection is
purely structural — backend identity from task metadata vs. configured
MCP servers — so it stays inside the ZFC envelope (mechanical comparison,
no semantic judgment).

Warning kinds emitted:

* ``backend_overlap`` — at least one config has access to a backend that
  produced the ground truth for at least one task.
* ``overshipping`` — informational task-level signal that one config
  submitted substantially more files than another while both recovered
  the oracle. After codeprobe-voxa the reward is recall (not F1), so
  over-shipping no longer drags the reward down — the warning exists so
  users can see *behavioural* differences (a "found everything plus the
  kitchen sink" config vs a tight config) without inferring a quality
  gap from a precision penalty that no longer exists.
* ``no_independent_baseline`` — every task's GT was produced by the same
  backend, and that backend is reachable from at least one config but not
  all of them. With no independent baseline, an aggregate "winner" is
  not a meaningful claim.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codeprobe.models.experiment import Experiment, ExperimentConfig

# MCP server name fragments that signal a Sourcegraph-style index backend.
# Matched as case-insensitive substring against ``mcp_config.mcpServers``
# keys so users can label their config "sourcegraph", "sg-prod", etc.
_SG_BACKEND_NAMES: tuple[str, ...] = ("sourcegraph", "sg")

# Org-scale MCP family categories whose ground truth is produced by
# Sourcegraph ``sg_find_references`` calls when ``--mcp-families`` is
# used during mining. See ``mining/org_scale.py``.
_SG_GT_CATEGORIES: frozenset[str] = frozenset(
    {
        "symbol-reference-trace",
        "type-hierarchy-consumers",
        "change-scope-audit",
    }
)

# Thresholds for the over-shipping informational pattern. After
# codeprobe-voxa reward is recall, so the trigger looks at *precision*
# rather than score gap: both configs achieved high recall, but one
# shipped substantially more files than the other. Documented constants —
# users can reason about exactly when a warning fires.
_OVERSHIPPING_RECALL_MIN = 0.95          # both configs found ~everything
_OVERSHIPPING_LOW_PRECISION_MAX = 0.5    # the over-shipper's precision
_OVERSHIPPING_PRECISION_GAP_MIN = 0.3    # min precision delta to flag


@dataclass(frozen=True)
class BiasWarning:
    """A single bias warning record.

    ``kind`` is a stable machine-readable category. ``message`` is the
    human-readable text printed before the score table. ``detail`` is an
    optional dict with structured fields useful for downstream tooling
    (e.g. CI gates that key off the JSON). ``severity`` distinguishes
    real measurement risks (``"warning"``) from honest signals that no
    longer apply because the multi-backend curator independently
    corroborated the GT (``"informational"``). The default is
    ``"warning"`` so existing detectors that don't compute severity stay
    in the warnings stream.
    """

    kind: str
    message: str
    detail: dict = field(default_factory=dict)
    severity: str = "warning"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "message": self.message,
            "detail": dict(self.detail),
        }


def config_backends(config: ExperimentConfig) -> frozenset[str]:
    """Return the set of MCP server names available to *config*.

    Names are lower-cased and normalised so callers can match against
    ``_SG_BACKEND_NAMES`` without worrying about user-chosen labels.
    """
    if not config.mcp_config:
        return frozenset()
    servers = config.mcp_config.get("mcpServers")
    if not isinstance(servers, dict):
        return frozenset()
    return frozenset(str(name).lower() for name in servers.keys())


def config_has_sg_backend(config: ExperimentConfig) -> bool:
    """True if *config*'s MCP surface includes a Sourcegraph-like backend."""
    backends = config_backends(config)
    for name in backends:
        for needle in _SG_BACKEND_NAMES:
            if needle in name:
                return True
    return False


def detect_task_gt_backends(task_dir: Path) -> frozenset[str]:
    """Identify which backends produced the ground truth for *task_dir*.

    Returns a frozenset of canonical backend names (e.g. ``"sourcegraph"``,
    ``"grep"``). Empty when the task carries no provenance signal — the
    safer fallback than guessing.

    Signals consulted (first match wins, then unioned):

    1. ``ground_truth.json`` ``oracle_backends_consensus`` — canonical
       semantic field emitted by the multi-backend oracle curator
       (codeprobe-zat9). Lists the backends that contributed >= 1 kept
       item to the answer key.
    2. ``ground_truth.json`` ``curation.backends_used`` — pre-existing
       curation provenance summary; same data, less specific name.
    3. ``tests/ground_truth_meta.json`` ``oracle_backends_consensus`` —
       CSB-style sidecar (CodeScaleBench writes provenance to a separate
       file alongside ``ground_truth.json``). Same field shape as (1).
    4. ``tests/ground_truth_meta.json`` ``backend`` — legacy CSB single
       string (``"local"`` | ``"deepsearch"`` | ``"hybrid"``); ``hybrid``
       is expanded to ``{"local", "deepsearch"}``.
    5. ``metadata.json`` ``metadata.sg_repo`` non-empty + category in the
       MCP family set → ``"sourcegraph"`` (the SG-driven mining path).
    6. ``metadata.json`` ``metadata.mcp_capabilities_at_mine_time`` — if
       it contains ``SYMBOL_REFERENCES``, GT is at least partially
       symbol-reference-driven; we report ``"sourcegraph"`` since that
       is the only registered SG-style backend today.
    """
    found: set[str] = set()

    gt_path = task_dir / "tests" / "ground_truth.json"
    if not gt_path.is_file():
        gt_path = task_dir / "ground_truth.json"
    if gt_path.is_file():
        try:
            gt = json.loads(gt_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            gt = None
        if isinstance(gt, dict):
            # codeprobe-zat9 canonical field — emitted by the
            # multi-backend oracle curator.
            obc = gt.get("oracle_backends_consensus")
            if isinstance(obc, list):
                for b in obc:
                    if isinstance(b, str) and b.strip():
                        found.add(b.strip().lower())
            # Legacy curation provenance summary.
            curation = gt.get("curation")
            if isinstance(curation, dict):
                backends = curation.get("backends_used")
                if isinstance(backends, list):
                    for b in backends:
                        if isinstance(b, str) and b.strip():
                            found.add(b.strip().lower())

    # CSB-style sidecar (codeprobe-zf3k cross-rig consistency). CSB writes
    # the curator agent's backend selection to ``ground_truth_meta.json``
    # rather than embedding it in ``ground_truth.json``. Read both keys.
    meta_sidecar_path = task_dir / "tests" / "ground_truth_meta.json"
    if meta_sidecar_path.is_file():
        try:
            sidecar = json.loads(meta_sidecar_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            sidecar = None
        if isinstance(sidecar, dict):
            obc = sidecar.get("oracle_backends_consensus")
            if isinstance(obc, list):
                for b in obc:
                    if isinstance(b, str) and b.strip():
                        found.add(b.strip().lower())
            backend = sidecar.get("backend")
            if isinstance(backend, str) and backend.strip():
                label = backend.strip().lower()
                if label == "hybrid":
                    found.update({"local", "deepsearch"})
                else:
                    found.add(label)

    meta_path = task_dir / "metadata.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            meta = None
        if isinstance(meta, dict):
            inner = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else meta
            sg_repo = inner.get("sg_repo") if isinstance(inner, dict) else None
            category = inner.get("category") if isinstance(inner, dict) else None
            if (
                isinstance(sg_repo, str)
                and sg_repo.strip()
                and isinstance(category, str)
                and category in _SG_GT_CATEGORIES
            ):
                found.add("sourcegraph")
            caps = inner.get("mcp_capabilities_at_mine_time") if isinstance(inner, dict) else None
            if isinstance(caps, list) and "SYMBOL_REFERENCES" in caps:
                if isinstance(category, str) and category in _SG_GT_CATEGORIES:
                    found.add("sourcegraph")

    return frozenset(found)


def _backend_matches(config_backends_set: frozenset[str], gt_backend: str) -> bool:
    """True if *gt_backend* is reachable from *config_backends_set*."""
    target = gt_backend.lower()
    if target in config_backends_set:
        return True
    if target in _SG_BACKEND_NAMES or any(needle in target for needle in _SG_BACKEND_NAMES):
        for name in config_backends_set:
            for needle in _SG_BACKEND_NAMES:
                if needle in name:
                    return True
    return False


def detect_backend_overlap(
    configs: list[ExperimentConfig],
    task_gt_backends: dict[str, frozenset[str]],
) -> list[BiasWarning]:
    """Flag configs whose tool surface includes a GT-producing backend.

    Severity gate (codeprobe-9re9): when at least one GT-producing
    backend in the affected tasks is *not* reachable by the config, the
    multi-backend curator has independent corroboration of the answer
    key. The agent's MCP surface still overlaps a GT backend, but the
    overlap is no longer load-bearing — flagging is honest signal, not
    a tautology risk. Severity is downgraded to ``"informational"`` so
    aggregate viewers can highlight only real measurement bias.
    """
    warnings: list[BiasWarning] = []
    all_gt_backends: set[str] = set()
    for backends in task_gt_backends.values():
        all_gt_backends.update(backends)
    if not all_gt_backends:
        return warnings

    for cfg in configs:
        cfg_backends = config_backends(cfg)
        if not cfg_backends:
            continue
        overlap = sorted(
            b for b in all_gt_backends if _backend_matches(cfg_backends, b)
        )
        if not overlap:
            continue
        affected = sorted(
            tid
            for tid, backends in task_gt_backends.items()
            if any(_backend_matches(cfg_backends, b) for b in backends)
        )
        # Independent corroboration: GT backends in the affected tasks
        # that the config cannot reach. If non-empty, downgrade severity.
        consensus_for_affected: set[str] = set()
        for tid in affected:
            consensus_for_affected.update(task_gt_backends.get(tid, frozenset()))
        independent_backends = sorted(
            b
            for b in consensus_for_affected
            if not _backend_matches(cfg_backends, b)
        )
        severity = "informational" if independent_backends else "warning"
        backend_label = ", ".join(overlap)
        if severity == "informational":
            independent_label = ", ".join(independent_backends)
            message = (
                f"{cfg.label!r} has access to {backend_label}, which "
                f"contributed to the ground truth — but the curator "
                f"independently corroborated GT via {independent_label}, so "
                f"this is informational rather than a tautology risk."
            )
        else:
            message = (
                f"{cfg.label!r} score may be tautological — ground truth was "
                f"built with {backend_label}, which is also available to the "
                f"agent via its MCP tool surface."
            )
        warnings.append(
            BiasWarning(
                kind="backend_overlap",
                severity=severity,
                message=message,
                detail={
                    "config": cfg.label,
                    "shared_backends": list(overlap),
                    "independent_backends": independent_backends,
                    "affected_task_count": len(affected),
                    "affected_tasks": affected,
                },
            )
        )
    return warnings


def detect_overshipping_anti_pattern(
    config_results: dict[str, list[dict]],
) -> list[BiasWarning]:
    """Flag informational over-shipping: same recall, different precision.

    Post-codeprobe-voxa reward is recall, so over-shipping no longer
    suppresses a config's score. The warning is now informational: it
    surfaces tasks where two configs both recovered the oracle (recall ≥
    threshold) but one submitted substantially more files than the other.
    Users see the *behavioural* difference; the reward is unaffected.
    """
    warnings: list[BiasWarning] = []
    labels = list(config_results.keys())
    if len(labels) < 2:
        return warnings

    by_task: dict[str, dict[str, dict]] = {}
    for label in labels:
        for row in config_results[label]:
            tid = row.get("task_id")
            if not isinstance(tid, str):
                continue
            by_task.setdefault(tid, {})[label] = row

    def _num(d: dict | None, key: str) -> float | None:
        if not isinstance(d, dict):
            return None
        v = d.get(key)
        return float(v) if isinstance(v, (int, float)) else None

    for tid, per_label in by_task.items():
        if len(per_label) < 2:
            continue
        labels_present = sorted(per_label.keys())
        for i, a_label in enumerate(labels_present):
            for b_label in labels_present[i + 1 :]:
                a = per_label[a_label]
                b = per_label[b_label]
                a_recall = _num(a.get("scoring_details"), "recall")
                b_recall = _num(b.get("scoring_details"), "recall")
                a_prec = _num(a.get("scoring_details"), "precision")
                b_prec = _num(b.get("scoring_details"), "precision")
                if (
                    a_recall is None
                    or b_recall is None
                    or a_prec is None
                    or b_prec is None
                ):
                    continue
                # Both must have recovered ~the entire oracle.
                if a_recall < _OVERSHIPPING_RECALL_MIN or b_recall < _OVERSHIPPING_RECALL_MIN:
                    continue
                # Identify over-shipper as the lower-precision side.
                if a_prec <= b_prec:
                    over, tight = a, b
                    over_label, tight_label = a_label, b_label
                    over_prec, tight_prec = a_prec, b_prec
                    over_recall = a_recall
                else:
                    over, tight = b, a
                    over_label, tight_label = b_label, a_label
                    over_prec, tight_prec = b_prec, a_prec
                    over_recall = b_recall
                if over_prec > _OVERSHIPPING_LOW_PRECISION_MAX:
                    continue
                if (tight_prec - over_prec) < _OVERSHIPPING_PRECISION_GAP_MIN:
                    continue
                warnings.append(
                    BiasWarning(
                        kind="overshipping",
                        message=(
                            f"task {tid!r}: {over_label!r} over-shipped vs "
                            f"{tight_label!r} (precision={over_prec:.2f} vs "
                            f"{tight_prec:.2f}); both recovered the oracle "
                            f"(recall={over_recall:.2f}). Reward is unaffected "
                            "(reward = recall); flagging as a behavioural "
                            "difference, not a quality gap."
                        ),
                        detail={
                            "task_id": tid,
                            "over_shipper_config": over_label,
                            "tight_config": tight_label,
                            "over_shipper_precision": over_prec,
                            "tight_precision": tight_prec,
                            "over_shipper_recall": over_recall,
                            "over_shipper_score": (
                                float(over["automated_score"])
                                if isinstance(over.get("automated_score"), (int, float))
                                else None
                            ),
                            "tight_score": (
                                float(tight["automated_score"])
                                if isinstance(tight.get("automated_score"), (int, float))
                                else None
                            ),
                        },
                    )
                )
    return warnings


def detect_no_independent_baseline(
    configs: list[ExperimentConfig],
    task_gt_backends: dict[str, frozenset[str]],
) -> list[BiasWarning]:
    """Refuse a winner when every task's GT comes from a single backend.

    Triggers only when:

    * every task carries a GT-backend signal, AND
    * the union across tasks is exactly one backend, AND
    * at least one config can reach that backend (i.e. tautology risk
      exists for some, not all, configs).

    The case where *no* config can reach the backend is a clean baseline
    — no overlap, no warning.
    """
    if not task_gt_backends:
        return []
    backends_per_task = list(task_gt_backends.values())
    if any(not s for s in backends_per_task):
        return []
    union: set[str] = set()
    for s in backends_per_task:
        union.update(s)
    if len(union) != 1:
        return []
    sole_backend = next(iter(union))

    matching = [c for c in configs if _backend_matches(config_backends(c), sole_backend)]
    if not matching:
        return []
    if len(matching) == len(configs):
        return []

    return [
        BiasWarning(
            kind="no_independent_baseline",
            message=(
                f"all tasks were mined with the same backend ({sole_backend!r}) "
                f"that produced their ground truth. Configs with access to that "
                f"backend ({', '.join(repr(c.label) for c in matching)}) face a "
                "tautology; configs without it cannot reach the GT independently. "
                "No independent baseline exists — winner suppressed; see "
                "per-config breakdown."
            ),
            detail={
                "sole_backend": sole_backend,
                "configs_with_backend": [c.label for c in matching],
                "configs_without_backend": [
                    c.label for c in configs if c not in matching
                ],
            },
        )
    ]


def collect_task_gt_backends(
    exp_dir: Path, tasks_subdir: str = "tasks"
) -> dict[str, frozenset[str]]:
    """Scan tasks/ and return a mapping of task_id → set of GT backends."""
    out: dict[str, frozenset[str]] = {}
    tasks_dir = exp_dir / tasks_subdir
    if not tasks_dir.is_dir():
        return out
    for task_dir in sorted(tasks_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        backends = detect_task_gt_backends(task_dir)
        if backends:
            out[task_dir.name] = backends
    return out


def detect_bias_warnings(
    experiment: Experiment,
    exp_dir: Path,
    config_results: dict[str, list[dict]],
) -> tuple[list[BiasWarning], dict[str, frozenset[str]]]:
    """Run all bias detectors and return warnings + the per-task GT map.

    Returning the GT map lets callers reuse it (e.g. to suppress winner
    ranking when ``no_independent_baseline`` fires) without re-scanning
    every task directory.
    """
    task_gt_backends = collect_task_gt_backends(exp_dir, experiment.tasks_dir)
    warnings: list[BiasWarning] = []
    warnings.extend(detect_backend_overlap(experiment.configs, task_gt_backends))
    warnings.extend(detect_overshipping_anti_pattern(config_results))
    warnings.extend(
        detect_no_independent_baseline(experiment.configs, task_gt_backends)
    )
    return warnings, task_gt_backends


__all__ = [
    "BiasWarning",
    "collect_task_gt_backends",
    "config_backends",
    "config_has_sg_backend",
    "detect_backend_overlap",
    "detect_bias_warnings",
    "detect_no_independent_baseline",
    "detect_overshipping_anti_pattern",
    "detect_task_gt_backends",
]
