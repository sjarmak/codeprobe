"""Codeprobe-side adapter that runs ``benchmark_qa_core`` checks against a task.

The QA library (``codeprobe.qa.benchmark_qa_core``) is rig-agnostic — every
adapter is responsible for parsing its own task-meta into the shapes the
checks expect. This module is codeprobe's implementation of that adapter:
read ``task.toml`` / ``metadata.json`` / ``tests/ground_truth.json`` and feed
already-extracted inputs into the three pure check functions.

Public API:

* :func:`verify_task_qa` — orchestrate all three checks for one task dir.
* :func:`load_task_meta`  — parse task metadata (TOML or JSON) into a flat
  dict, exposed for callers that want to inspect the metadata directly.

Class-D (path include / exclude) findings are suppressed by default. The
codeprobe scoring layer already enforces path scope through
``oracle_overlap_recall`` / ``oracle_overlap_fbeta`` reward weighting (see
``core/scoring.py``); raising both a Class-D error and a low score on the
same task is double-reporting. Callers that want path findings anyway can
flip ``suppress_class_d=False``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from codeprobe.qa.benchmark_qa_core import (
    Finding,
    OracleConstraints,
    check_aux_file_leakage,
    check_oracle_coherence,
    check_scoring_honesty,
)

__all__ = [
    "QAVerifyResult",
    "load_task_meta",
    "verify_task_qa",
]


# Tier labels are the lib's contract: empty strings or "unknown" trigger an
# E3 warning; missing keys trigger E2. Codeprobe's scoring methods map onto
# the four lib-sanctioned tiers so existing checks stay green.
DEFAULT_SCORING_METHOD_TIERS: dict[str, str] = {
    # Direct-leg scorers (test.sh)
    "binary": "strict",
    "exact_match": "strict",
    "continuous": "calibrated",
    # Artifact-leg scorers (answer.json vs ground_truth.json)
    "file_list": "calibrated",
    "symbol_list": "calibrated",
    "count": "strict",
    "boolean": "strict",
    "text": "strict",
    "dependency_chain": "calibrated",
    # Composite
    "dual": "calibrated",
    "weighted_checkpoints": "calibrated",
}


# Map between codeprobe metadata language tokens and benchmark_qa_core's
# expected lower-cased ones. Keep small — additions go through a code
# review, not a config file, since "is X a real language for this task?"
# is policy.
_LANGUAGE_NORMALISE: dict[str, str] = {
    "py": "python",
    "python": "python",
    "ts": "typescript",
    "typescript": "typescript",
    "js": "javascript",
    "javascript": "javascript",
    "go": "go",
    "rust": "rust",
    "java": "java",
    "kotlin": "kotlin",
    "swift": "swift",
    "ruby": "ruby",
    "php": "php",
    "cpp": "cpp",
    "c++": "cpp",
    "c": "c",
    "csharp": "csharp",
    "c#": "csharp",
}


@dataclass(frozen=True)
class QAVerifyResult:
    """Outcome of running the QA checks against one task directory.

    ``findings`` is the flat list returned by the underlying lib. ``ok`` is
    True iff no finding is at ``error`` severity — warnings and infos do
    not block. ``task_dir`` is preserved so callers iterating over many
    tasks don't lose track of which one a finding belongs to.
    """

    task_dir: Path
    findings: list[Finding]

    @property
    def ok(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)

    @property
    def error_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "warning")


def load_task_meta(task_dir: Path) -> dict:
    """Return parsed task metadata as a flat dict.

    Reads ``task.toml`` first, falling back to ``metadata.json``. Returns
    an empty dict when neither exists or both fail to parse — callers
    typically check the return for missing required keys (e.g.
    ``scoring_method``) and surface Finding-level errors. Both formats
    are flattened the same way (top-level + one-deep section keys merged
    into a single namespace) so the QA lib doesn't need to know about
    section nesting.
    """
    toml_path = task_dir / "task.toml"
    if toml_path.is_file():
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover — py310 fallback
            import tomli as tomllib  # type: ignore[no-redef,import-not-found]
        try:
            with toml_path.open("rb") as f:
                data = tomllib.load(f)
        except (OSError, ValueError):
            return {}
        return _flatten_sections(data)

    json_path = task_dir / "metadata.json"
    if json_path.is_file():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return _flatten_sections(data)

    return {}


def verify_task_qa(
    task_dir: Path,
    *,
    repo_root: Path | None = None,
    aux_files: Iterable[Path] | None = None,
    scoring_method_tiers: dict[str, str] | None = None,
    suppress_class_d: bool = True,
    require_symbols_resolve: bool = False,
) -> QAVerifyResult:
    """Run all three benchmark_qa_core checks against ``task_dir``.

    The task dir is expected to follow the standard codeprobe layout::

        <task_dir>/
            task.toml | metadata.json
            instruction.md
            tests/ground_truth.json

    Args:
        task_dir: Path to the task directory to verify.
        repo_root: Repo to resolve oracle file paths against. Falls back to
            ``task_dir`` itself when None — the conservative default for
            codeprobe layouts where the agent's working dir is the task
            sandbox. Mined tasks that need to resolve against the original
            repo should pass it explicitly.
        aux_files: Files visible to the agent during the task. The leakage
            check reads each one looking for oracle tokens. Defaults to a
            short list of standard codeprobe context files (instruction.md,
            anything under ``context/``).
        scoring_method_tiers: Override the default tier table. Helpful for
            CSB / EB callers that recognise different methods than
            codeprobe; leave as None for the codeprobe defaults.
        suppress_class_d: Drop ``D1`` and ``D2`` findings before returning.
            On by default because codeprobe's ScoreResult contract already
            penalises off-scope answers; double-reporting them as QA
            errors creates noise without adding signal.
        require_symbols_resolve: When True, missing oracle symbols are
            ``error`` rather than ``warning``. Off by default — codeprobe
            often mines symbol oracles loosely.

    Returns:
        A :class:`QAVerifyResult` with the flat finding list and a derived
        ``ok`` flag (``True`` iff zero error-severity findings).
    """
    task_dir = Path(task_dir)
    if not task_dir.is_dir():
        return QAVerifyResult(
            task_dir=task_dir,
            findings=[
                Finding(
                    severity="error",
                    code="A1",
                    message=f"Task directory does not exist: {task_dir}",
                    location=str(task_dir),
                )
            ],
        )

    meta_flat = load_task_meta(task_dir)
    instruction_text = _read_instruction(task_dir)
    ground_truth = _read_ground_truth(task_dir)

    oracle_files = _extract_oracle_files(ground_truth)
    oracle_symbols = _extract_oracle_symbols(ground_truth)
    oracle_tokens = _extract_oracle_tokens(ground_truth)

    constraints = _build_oracle_constraints(
        meta_flat,
        require_symbols_resolve=require_symbols_resolve,
    )

    findings: list[Finding] = []

    findings.extend(
        check_oracle_coherence(
            instruction_text=instruction_text,
            oracle_files=oracle_files,
            oracle_symbols=oracle_symbols,
            repo_root=Path(repo_root) if repo_root is not None else task_dir,
            constraints=constraints,
        )
    )

    findings.extend(
        check_scoring_honesty(
            task_meta=_meta_for_scoring_check(meta_flat),
            scoring_method_tiers=scoring_method_tiers or DEFAULT_SCORING_METHOD_TIERS,
        )
    )

    aux_paths = (
        list(aux_files) if aux_files is not None else _default_aux_files(task_dir)
    )
    findings.extend(
        check_aux_file_leakage(
            oracle_tokens=oracle_tokens,
            aux_files=aux_paths,
        )
    )

    if suppress_class_d:
        findings = [f for f in findings if f.code not in {"D1", "D2"}]

    return QAVerifyResult(task_dir=task_dir, findings=findings)


def _flatten_sections(data: dict) -> dict:
    """Flatten a parsed task.toml / metadata.json into one key namespace.

    The on-disk schema nests fields under ``[metadata]`` /
    ``[verification]`` / ``[task]`` (TOML) or under ``"metadata"`` /
    ``"verification"`` / ``"task"`` (JSON), but the QA lib's
    ``check_scoring_honesty`` only looks at the top level. This helper
    merges those sections (with later keys winning) so adapter callers
    don't need to know the layout. Top-level scalars survive unchanged.
    """
    flat: dict = {}
    for key, value in data.items():
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                flat[sub_key] = sub_value
        else:
            flat[key] = value
    return flat


def _meta_for_scoring_check(meta_flat: dict) -> dict:
    """Project the codeprobe metadata onto the QA lib's expected shape.

    The lib expects a ``scoring_method`` field. Codeprobe stores this under
    ``reward_type`` (canonical name in ``[verification]``). We surface
    ``reward_type`` as ``scoring_method`` when the legacy field is absent
    so existing tasks don't all flag E1.
    """
    if "scoring_method" in meta_flat:
        return dict(meta_flat)
    projection = dict(meta_flat)
    if "reward_type" in meta_flat:
        projection["scoring_method"] = meta_flat["reward_type"]
    return projection


def _read_instruction(task_dir: Path) -> str:
    path = task_dir / "instruction.md"
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _read_ground_truth(task_dir: Path) -> dict:
    """Read tests/ground_truth.json, returning ``{}`` on any failure."""
    for candidate in (
        task_dir / "tests" / "ground_truth.json",
        task_dir / "ground_truth.json",
    ):
        if candidate.is_file():
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
            if isinstance(payload, dict):
                return payload
    return {}


def _extract_oracle_files(ground_truth: dict) -> list[str]:
    """Pull repo-relative file paths out of ground_truth.json.

    Supports the three formats codeprobe ground_truth.json can take:

    * v2 ``checks`` array — pull ``answer`` from each ``answer_type ==
      file_list`` check.
    * v1 ``answer_type`` + ``answer`` — pull when ``answer_type ==
      file_list``.
    * legacy ``expected`` list — treat as file_list directly.
    """
    if "checks" in ground_truth:
        files: list[str] = []
        checks = ground_truth.get("checks") or []
        if not isinstance(checks, list):
            return []
        for check in checks:
            if not isinstance(check, dict):
                continue
            if check.get("answer_type") == "file_list":
                ans = check.get("answer")
                if isinstance(ans, list):
                    files.extend(str(p) for p in ans if isinstance(p, str))
        return files

    if ground_truth.get("answer_type") == "file_list":
        ans = ground_truth.get("answer")
        if isinstance(ans, list):
            return [str(p) for p in ans if isinstance(p, str)]

    if isinstance(ground_truth.get("expected"), list):
        return [str(p) for p in ground_truth["expected"] if isinstance(p, str)]

    return []


def _extract_oracle_symbols(ground_truth: dict) -> list[tuple[str, str]]:
    """Return ``(file, symbol)`` pairs from a symbol_list answer, if any.

    Codeprobe's symbol_list payload is a flat list of symbol names rather
    than ``(file, symbol)`` pairs. The QA lib needs file context to
    resolve, so we surface symbols only when they're explicitly attached
    to a file in the v2 ``oracle_metadata.symbol_files`` map (mined-tier
    convention). Bare symbol_list answers map to empty pairs and rely on
    the lib's other checks.
    """
    pairs: list[tuple[str, str]] = []
    metadata = ground_truth.get("oracle_metadata") or {}
    if isinstance(metadata, dict):
        symbol_files = metadata.get("symbol_files")
        if isinstance(symbol_files, dict):
            for file_path, symbols in symbol_files.items():
                if not isinstance(file_path, str) or not isinstance(symbols, list):
                    continue
                for sym in symbols:
                    if isinstance(sym, str):
                        pairs.append((file_path, sym))
    return pairs


def _extract_oracle_tokens(ground_truth: dict) -> list[str]:
    """Materialise leakage-check tokens from the ground truth answer.

    For file_list / symbol_list / dependency_chain answers, the tokens are
    the answer entries themselves. For scalar answers (count / boolean /
    text), we treat the stringified value as a token only when it looks
    distinctive (length >= 3) — short scalars are filtered by the lib's
    F1 finding anyway.
    """
    tokens: list[str] = []
    seen: set[str] = set()

    def _add(token: object) -> None:
        if not isinstance(token, str):
            token = str(token)
        if not token:
            return
        if token in seen:
            return
        seen.add(token)
        tokens.append(token)

    def _from_check(check: dict) -> None:
        ans = check.get("answer")
        atype = check.get("answer_type", "")
        if atype in {"file_list", "symbol_list", "dependency_chain"} and isinstance(
            ans, list
        ):
            for entry in ans:
                _add(entry)
        elif atype in {"count", "boolean", "text"} and ans is not None:
            _add(ans)

    if "checks" in ground_truth:
        for check in ground_truth.get("checks") or []:
            if isinstance(check, dict):
                _from_check(check)
    elif "answer_type" in ground_truth:
        _from_check(ground_truth)
    elif isinstance(ground_truth.get("expected"), list):
        for entry in ground_truth["expected"]:
            _add(entry)

    return tokens


def _build_oracle_constraints(
    meta_flat: dict,
    *,
    require_symbols_resolve: bool,
) -> OracleConstraints:
    """Map codeprobe metadata onto :class:`OracleConstraints`.

    Today we only resolve ``expected_languages`` from the metadata
    ``language`` field. Path include/exclude is left empty: codeprobe
    tasks don't carry a declarative path scope, and the scoring layer
    already enforces it via reward weighting.
    """
    languages: set[str] = set()
    raw_lang = meta_flat.get("language")
    if isinstance(raw_lang, str):
        normalised = _LANGUAGE_NORMALISE.get(raw_lang.strip().lower())
        if normalised:
            languages.add(normalised)
    elif isinstance(raw_lang, (list, tuple)):
        for entry in raw_lang:
            if isinstance(entry, str):
                normalised = _LANGUAGE_NORMALISE.get(entry.strip().lower())
                if normalised:
                    languages.add(normalised)

    return OracleConstraints(
        expected_languages=frozenset(languages),
        path_include=(),
        path_exclude=(),
        require_symbols_resolve=require_symbols_resolve,
    )


def _default_aux_files(task_dir: Path) -> list[Path]:
    """Standard codeprobe aux-file list: instruction.md plus context/."""
    files: list[Path] = []
    instruction = task_dir / "instruction.md"
    if instruction.is_file():
        files.append(instruction)
    context_dir = task_dir / "context"
    if context_dir.is_dir():
        files.extend(p for p in context_dir.rglob("*") if p.is_file())
    return files
