"""Verifier-honesty lint for codeprobe scorers (Class B from CSB plan).

The Code Scale Bench verification report (2026-04-24) shipped shellcheck +
custom rules over its 781 ``test.sh`` files. codeprobe's verifiers are
Python, so the analogous lint is an AST scan over the scorer modules. The
goal is to catch four classes of *verifier dishonesty* that have shipped
in the past or could ship silently going forward:

1. **Missing ``scorer_family`` declaration** — every ``ScoreResult(...)``
   constructor returned at the top level of a scorer must declare which
   rubric produced the reward. Empty-string families are interpretable
   only as "unknown rubric"; they break downstream tooling that joins
   per-task reward with the family. The voxa bug regressed because a
   ``ScoreResult`` returned ``score = recall`` without ever declaring
   the family the caller requested.

2. **Quiet recall fallbacks** — ``_derive_reward_and_metrics`` resolves
   the headline reward by family (F1, fbeta, recall, weighted_*).
   When the requested family's primary metric is missing, the function
   falls back to a different metric. F1-family fallbacks to *recall* /
   *weighted_recall* are dishonest: the caller asked for a precision-
   sensitive reward and got a precision-blind one. The lint flags these
   shapes; honest fallbacks (recall family → recall, fbeta with beta=1
   → f1) stay silent.

3. **Hardcoded numeric thresholds** — float thresholds inlined into
   scorer / bias-detector modules without a named constant or config
   import are policy hidden in code. Per ZFC, thresholds are policy and
   must live in declared, named constants (or a config layer). The lint
   accepts named module-level constants (the bias-detection
   ``_OVERSHIPPING_*`` constants are honest because they are explicitly
   named and documented in the module docstring); it flags raw float
   literals embedded in expressions.

4. **Bare ``except`` in scorer code** — silent ``except: return 0.0``
   patterns swallow root causes and turn scorer bugs into "agent failed
   the task". Per architecture.md §"Don't Swallow Errors". Scorer
   exception paths must catch specific types and surface a real error.

The lint is a pytest test rather than a separate CLI so it runs in the
default ``pytest`` invocation and produces a regression-style report.
Pre-existing offenders documented in the bead description and in
CLAUDE.md ZFC notes are tracked in ``_KNOWN_OFFENDERS`` with the bead
ID(s) that follow up; a new offender either gains an explicit waiver
(reviewer judgment) or fails CI.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from codeprobe.core.scoring import SCORER_FAMILIES

# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
SCORING_FILE = REPO_ROOT / "src" / "codeprobe" / "core" / "scoring.py"
BIAS_FILE = REPO_ROOT / "src" / "codeprobe" / "core" / "bias_detection.py"


# ---------------------------------------------------------------------------
# Known offenders — tracked, not silenced
# ---------------------------------------------------------------------------
#
# Each entry is a (file, line_start, line_end, rule, reason, follow_up_bead)
# tuple. ``rule`` is the short rule code emitted by the lint analyzers
# below; ``reason`` is a one-line human explanation; ``follow_up_bead`` is
# the bead ID that tracks fixing the offender (or "" when filed inline as
# part of the lint-landing commit).
#
# When you fix an offender, DELETE its entry. Adding a new entry requires
# reviewer sign-off — the lint is a CI gate, not a suggestion.

OffenderKey = tuple[str, str, int]  # (relpath, rule, line)


@dataclass(frozen=True)
class Offender:
    relpath: str
    rule: str
    line_start: int
    line_end: int
    reason: str
    follow_up_bead: str


_KNOWN_OFFENDERS: tuple[Offender, ...] = (
    # Quiet recall fallbacks under F1-family.
    #
    # ``_derive_reward_and_metrics`` resolves reward by family. Under
    # ``oracle_weighted_f1`` the function falls back to ``weighted_recall``
    # (line ~740) and then to ``recall`` (line ~743) when ``f1`` is
    # absent in metrics.json. That is the voxa-class regression: the
    # caller asked for an F1-family reward and got recall. The honest
    # fix is to fail closed (or compute F1 from precision+recall when
    # both are present), not silently degrade to recall.
    Offender(
        relpath="src/codeprobe/core/scoring.py",
        rule="quiet-recall-fallback",
        line_start=732,
        line_end=750,
        reason=(
            "ContinuousScorer._derive_reward_and_metrics: oracle_weighted_f1 "
            "branch falls back to weighted_recall / recall when f1 is missing "
            "from metrics.json. Family says F1 but reward becomes recall."
        ),
        follow_up_bead="codeprobe-jdkq-followup-weighted-f1-fallback",
    ),
    # Bias-detection hardcoded thresholds.
    #
    # The ``_OVERSHIPPING_*`` constants in ``bias_detection.py`` are
    # *named* and documented, but they are still magic numbers wired
    # into the detector without config plumbing. ZFC says thresholds
    # are policy; they should be sourced from a config or per-experiment
    # declaration so users can tune sensitivity without editing code.
    Offender(
        relpath="src/codeprobe/core/bias_detection.py",
        rule="hardcoded-threshold",
        line_start=60,
        line_end=60,
        reason=(
            "_OVERSHIPPING_RECALL_MIN named module constant, not config-plumbed."
        ),
        follow_up_bead="codeprobe-jdkq-followup-bias-thresholds-config",
    ),
    Offender(
        relpath="src/codeprobe/core/bias_detection.py",
        rule="hardcoded-threshold",
        line_start=61,
        line_end=61,
        reason=(
            "_OVERSHIPPING_LOW_PRECISION_MAX named module constant, not "
            "config-plumbed."
        ),
        follow_up_bead="codeprobe-jdkq-followup-bias-thresholds-config",
    ),
    Offender(
        relpath="src/codeprobe/core/bias_detection.py",
        rule="hardcoded-threshold",
        line_start=62,
        line_end=62,
        reason=(
            "_OVERSHIPPING_PRECISION_GAP_MIN named module constant, not "
            "config-plumbed."
        ),
        follow_up_bead="codeprobe-jdkq-followup-bias-thresholds-config",
    ),
    # ArtifactScorer low-confidence warning threshold. Promoted from an
    # inline ``< 0.5`` literal to a named module constant during the
    # codeprobe-jdkq commit; the named form is honest documentation but
    # still hardcoded. Same follow-up bead covers the config plumbing.
    Offender(
        relpath="src/codeprobe/core/scoring.py",
        rule="hardcoded-threshold",
        line_start=1220,
        line_end=1230,
        reason=(
            "_LOW_CONFIDENCE_THRESHOLD warns when ground_truth.confidence "
            "< 0.5. Named, documented, but not config-sourced."
        ),
        follow_up_bead="codeprobe-jdkq-followup-bias-thresholds-config",
    ),
)


def _offender_for(
    relpath: str, rule: str, line: int
) -> Offender | None:
    for o in _KNOWN_OFFENDERS:
        if o.relpath != relpath:
            continue
        if o.rule != rule:
            continue
        if o.line_start <= line <= o.line_end:
            return o
    return None


# ---------------------------------------------------------------------------
# Finding shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    relpath: str
    line: int
    rule: str
    detail: str

    def format(self) -> str:
        return f"{self.relpath}:{self.line} [{self.rule}] {self.detail}"


def _relpath(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


# ---------------------------------------------------------------------------
# Rule 1 — every ScoreResult(...) constructor must declare scorer_family
# ---------------------------------------------------------------------------


def _is_scoreresult_call(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "ScoreResult"
    if isinstance(func, ast.Attribute):
        return func.attr == "ScoreResult"
    return False


def _scoreresult_kwargs(call: ast.Call) -> dict[str, ast.expr]:
    out: dict[str, ast.expr] = {}
    for kw in call.keywords:
        if kw.arg is None:
            continue
        out[kw.arg] = kw.value
    return out


def _is_truthy_string(node: ast.expr) -> bool:
    """Best-effort static check that *node* is a non-empty string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return bool(node.value)
    # ``self.SCORER_FAMILY`` / ``cls.SCORER_FAMILY`` / module-level vars
    # — assume truthy. The lint can't statically prove they are
    # non-empty without executing the module; the sub-rule "must
    # declare" is satisfied by the kwarg being present at all.
    if isinstance(node, ast.Attribute):
        return True
    if isinstance(node, ast.Name):
        return True
    return False


def find_missing_scorer_family(source: str, relpath: str) -> list[Finding]:
    """Flag ``ScoreResult(...)`` calls without a ``scorer_family=`` kwarg.

    This is the core check: every scoring path that surfaces a result to
    the caller MUST declare which rubric produced the reward. Empty
    strings still pass — that is a documented opaque value — but the
    kwarg must be present so reviewers see the declaration.
    """
    findings: list[Finding] = []
    tree = ast.parse(source, filename=relpath)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_scoreresult_call(node):
            continue
        kwargs = _scoreresult_kwargs(node)
        if "scorer_family" in kwargs:
            continue
        # ``**kwargs`` expansion is opaque; assume the caller knows.
        if any(kw.arg is None for kw in node.keywords):
            continue
        findings.append(
            Finding(
                relpath=relpath,
                line=node.lineno,
                rule="missing-scorer-family",
                detail=(
                    "ScoreResult(...) constructed without scorer_family= "
                    "— declare the rubric that produced the reward"
                ),
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Rule 2 — quiet recall fallbacks
# ---------------------------------------------------------------------------
#
# Pattern: ``reward = recall`` (or ``= weighted_recall``) inside an
# ``if family == "oracle_overlap_f1"`` / ``"oracle_weighted_f1"`` /
# ``"oracle_overlap_fbeta"`` branch — the caller asked for a precision-
# sensitive metric and the code degrades to recall.
#
# We detect this by walking each function body: when we see a
# ``family == <F1-ish family>`` comparison guarding a block, we scan
# the block for ``reward = recall`` / ``reward = weighted_recall``
# assignments without a guarding "we have precision and recall" check.

_F1_FAMILY_LITERALS: frozenset[str] = frozenset(
    {
        "oracle_overlap_f1",
        "oracle_overlap_fbeta",
        "oracle_weighted_f1",
    }
)

_RECALL_NAMES: frozenset[str] = frozenset({"recall", "weighted_recall"})


def _branch_compares_family_to(node: ast.If, target: frozenset[str]) -> bool:
    """True when ``node.test`` is ``family == "<family literal>"``."""
    test = node.test
    if not isinstance(test, ast.Compare):
        return False
    if not (
        isinstance(test.left, ast.Name)
        and test.left.id == "family"
    ):
        return False
    if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    rhs = test.comparators[0]
    if not (isinstance(rhs, ast.Constant) and isinstance(rhs.value, str)):
        return False
    return rhs.value in target


def _find_recall_assignments(body: list[ast.stmt]) -> list[ast.Assign]:
    """Collect ``reward = recall`` / ``reward = weighted_recall`` shapes."""
    out: list[ast.Assign] = []
    for stmt in body:
        for sub in ast.walk(stmt):
            if not isinstance(sub, ast.Assign):
                continue
            if not (len(sub.targets) == 1 and isinstance(sub.targets[0], ast.Name)):
                continue
            if sub.targets[0].id != "reward":
                continue
            value = sub.value
            # ``reward = recall``
            if isinstance(value, ast.Name) and value.id in _RECALL_NAMES:
                out.append(sub)
                continue
            # ``reward = recall if recall is not None else ...``
            if isinstance(value, ast.IfExp):
                body_val = value.body
                if isinstance(body_val, ast.Name) and body_val.id in _RECALL_NAMES:
                    out.append(sub)
                    continue
    return out


def find_quiet_recall_fallbacks(source: str, relpath: str) -> list[Finding]:
    """Flag F1-family branches that fall through to ``reward = recall``.

    Honest fallbacks (recall family → recall, fbeta with beta=1 → f1)
    are NOT flagged — the lint targets the specific shape "family says
    precision-sensitive, code returns recall".
    """
    findings: list[Finding] = []
    tree = ast.parse(source, filename=relpath)

    # Walk ``If`` / ``ElIf`` chains. Each ``If`` node represents one
    # branch; the ``orelse`` chain holds the elif / else continuation.
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        if not _branch_compares_family_to(node, _F1_FAMILY_LITERALS):
            continue
        for assign in _find_recall_assignments(node.body):
            findings.append(
                Finding(
                    relpath=relpath,
                    line=assign.lineno,
                    rule="quiet-recall-fallback",
                    detail=(
                        "F1-family branch assigns reward = recall/"
                        "weighted_recall — caller asked for a precision-"
                        "sensitive reward; recall is precision-blind"
                    ),
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Rule 3 — hardcoded numeric thresholds (inline + module-level constants)
# ---------------------------------------------------------------------------
#
# ZFC says thresholds are policy. Two violation shapes are flagged:
#
#   3a. Inline float literal in a comparison inside a function body.
#       e.g. ``if confidence < 0.5:`` — the ``0.5`` is invisible policy.
#       Fix: promote to a named module constant or pass through config.
#
#   3b. Module-level constant whose name suggests it is a threshold and
#       whose value is a float literal. The name is honest documentation
#       (better than inline) but the value still doesn't flow from
#       config. Fix: source from ``ExperimentConfig`` / task metadata.
#       Tracked via ``_KNOWN_OFFENDERS`` while the config plumbing is
#       designed; fresh additions still fail the lint.
#
# Allowed shapes:
#   * Clamps: ``max(0.0, min(1.0, x))`` — bounds are not policy.
#   * Integer literals (0, 1) and the explicit floats {0.0, 1.0, 2.0}.

_ALLOWED_FLOAT_LITERALS: frozenset[float] = frozenset({0.0, 1.0, 2.0})

# Module-level constants whose name matches one of these suffixes/contains
# tokens are treated as threshold declarations (rule 3b).
_THRESHOLD_NAME_TOKENS: tuple[str, ...] = (
    "_THRESHOLD",
    "_MIN",
    "_MAX",
    "_GAP",
    "_LIMIT",
)


def _is_threshold_name(name: str) -> bool:
    upper = name.upper()
    for token in _THRESHOLD_NAME_TOKENS:
        if upper.endswith(token):
            return True
    return False


def _module_level_constant_lines(tree: ast.Module) -> set[int]:
    """Return line numbers of module-level numeric assignments.

    Used to exempt ``_THRESHOLD = 0.5`` style declarations from the
    inline-threshold rule (rule 3a). Rule 3b handles them separately.
    """
    out: set[int] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            if isinstance(stmt.value, ast.Constant) and isinstance(
                stmt.value.value, (int, float)
            ):
                out.add(stmt.lineno)
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            if isinstance(stmt.value, ast.Constant) and isinstance(
                stmt.value.value, (int, float)
            ):
                out.add(stmt.lineno)
    return out


def _is_float_threshold(node: ast.AST) -> float | None:
    """Return the literal value if *node* is a non-allowed float in (0,1)."""
    if not isinstance(node, ast.Constant):
        return None
    val = node.value
    if not isinstance(val, float):
        return None
    if val in _ALLOWED_FLOAT_LITERALS:
        return None
    if 0.0 < val < 1.0:
        return val
    return None


def _module_level_threshold_constants(
    tree: ast.Module,
) -> list[tuple[str, float, int]]:
    """Return ``[(name, value, lineno), ...]`` for module-level threshold
    constants — assignments whose target name matches a threshold token
    and whose value is a float literal.
    """
    out: list[tuple[str, float, int]] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            if not isinstance(stmt.value, ast.Constant):
                continue
            if not isinstance(stmt.value.value, float):
                continue
            for target in stmt.targets:
                if isinstance(target, ast.Name) and _is_threshold_name(target.id):
                    out.append((target.id, stmt.value.value, stmt.lineno))
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            if not isinstance(stmt.value, ast.Constant):
                continue
            if not isinstance(stmt.value.value, float):
                continue
            target = stmt.target
            if isinstance(target, ast.Name) and _is_threshold_name(target.id):
                out.append((target.id, stmt.value.value, stmt.lineno))
    return out


def find_hardcoded_thresholds(source: str, relpath: str) -> list[Finding]:
    """Flag inline float-literal thresholds AND module-level threshold
    constants that are hardcoded rather than sourced from config.
    """
    findings: list[Finding] = []
    tree = ast.parse(source, filename=relpath)
    constant_lines = _module_level_constant_lines(tree)

    # Rule 3a — inline literals in function bodies.
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            if isinstance(node, ast.Compare):
                for operand in [node.left, *node.comparators]:
                    val = _is_float_threshold(operand)
                    if val is None:
                        continue
                    if operand.lineno in constant_lines:
                        continue
                    findings.append(
                        Finding(
                            relpath=relpath,
                            line=operand.lineno,
                            rule="hardcoded-threshold",
                            detail=(
                                f"inline float threshold {val!r} in compare "
                                f"— promote to a named module constant or "
                                f"config field"
                            ),
                        )
                    )

    # Rule 3b — module-level threshold-named float constants.
    for name, value, lineno in _module_level_threshold_constants(tree):
        findings.append(
            Finding(
                relpath=relpath,
                line=lineno,
                rule="hardcoded-threshold",
                detail=(
                    f"module-level threshold constant {name}={value!r} is "
                    f"hardcoded — source from config / task metadata so "
                    f"users can tune sensitivity without editing code"
                ),
            )
        )

    return findings


# ---------------------------------------------------------------------------
# Rule 4 — bare except: in scorer code
# ---------------------------------------------------------------------------


def find_bare_excepts(source: str, relpath: str) -> list[Finding]:
    """Flag bare ``except:`` and ``except Exception:`` patterns.

    ``except Exception:`` is allowed only when explicitly annotated with
    ``# noqa`` — a few load-bearing places in the codebase need a
    catch-all (e.g. DualScorer's ``_safe_leg_score``). The lint accepts
    the ``# noqa`` line marker so reviewers see the declaration.
    """
    findings: list[Finding] = []
    tree = ast.parse(source, filename=relpath)
    source_lines = source.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        # Bare except: (no exception type at all).
        if node.type is None:
            line_text = source_lines[node.lineno - 1] if node.lineno >= 1 else ""
            if "noqa" in line_text:
                continue
            findings.append(
                Finding(
                    relpath=relpath,
                    line=node.lineno,
                    rule="bare-except",
                    detail="bare except: — catch a specific exception type",
                )
            )
            continue
        # except Exception: — accepted only when noqa-annotated.
        if isinstance(node.type, ast.Name) and node.type.id == "Exception":
            line_text = source_lines[node.lineno - 1] if node.lineno >= 1 else ""
            if "noqa" in line_text:
                continue
            findings.append(
                Finding(
                    relpath=relpath,
                    line=node.lineno,
                    rule="bare-except",
                    detail=(
                        "except Exception: without # noqa — narrow to "
                        "specific exception types or annotate with noqa "
                        "and a one-line justification"
                    ),
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Combined runner
# ---------------------------------------------------------------------------


def _run_lint(path: Path) -> list[Finding]:
    source = path.read_text(encoding="utf-8")
    relpath = _relpath(path)
    findings: list[Finding] = []
    findings.extend(find_missing_scorer_family(source, relpath))
    findings.extend(find_quiet_recall_fallbacks(source, relpath))
    findings.extend(find_hardcoded_thresholds(source, relpath))
    findings.extend(find_bare_excepts(source, relpath))
    return findings


def _split_findings(
    findings: list[Finding],
) -> tuple[list[Finding], list[tuple[Finding, Offender]]]:
    """Split findings into (unsuppressed, allowlisted)."""
    fresh: list[Finding] = []
    suppressed: list[tuple[Finding, Offender]] = []
    for f in findings:
        offender = _offender_for(f.relpath, f.rule, f.line)
        if offender is None:
            fresh.append(f)
        else:
            suppressed.append((f, offender))
    return fresh, suppressed


# ---------------------------------------------------------------------------
# Pytest entry points
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [SCORING_FILE, BIAS_FILE],
    ids=lambda p: p.name,
)
def test_scorer_honesty_no_new_offenders(target: Path) -> None:
    """Lint each scorer module — fail on any new (un-allowlisted) finding."""
    assert target.is_file(), f"lint target missing: {target}"
    findings = _run_lint(target)
    fresh, _suppressed = _split_findings(findings)
    if fresh:
        msg_lines = [f.format() for f in fresh]
        pytest.fail(
            "scorer-honesty lint found new violations — fix them, or "
            "(only with reviewer sign-off) extend _KNOWN_OFFENDERS in "
            "tests/lint/test_scorer_honesty.py.\n"
            + "\n".join(msg_lines)
        )


def test_scorer_honesty_known_offenders_still_present() -> None:
    """Allowlist hygiene — every known offender must still be detectable.

    If a known offender disappears (refactor lands), its allowlist entry
    is dead and should be removed in the same PR. This test fails when
    that cleanup is missed, forcing maintenance of the allowlist.
    """
    findings: list[Finding] = []
    for target in (SCORING_FILE, BIAS_FILE):
        if not target.is_file():
            continue
        findings.extend(_run_lint(target))

    matched_offenders: set[tuple[str, str, int, int]] = set()
    for f in findings:
        offender = _offender_for(f.relpath, f.rule, f.line)
        if offender is not None:
            matched_offenders.add(
                (
                    offender.relpath,
                    offender.rule,
                    offender.line_start,
                    offender.line_end,
                )
            )

    dead: list[Offender] = []
    for o in _KNOWN_OFFENDERS:
        key = (o.relpath, o.rule, o.line_start, o.line_end)
        if key not in matched_offenders:
            dead.append(o)

    if dead:
        pytest.fail(
            "Known-offender allowlist is stale — these entries no longer "
            "match a finding. Delete them from _KNOWN_OFFENDERS:\n"
            + "\n".join(
                f"  {o.relpath}:{o.line_start}-{o.line_end} [{o.rule}] "
                f"{o.reason}"
                for o in dead
            )
        )


def test_lint_catches_missing_scorer_family_in_synthetic_source() -> None:
    """Smoke test — the rule fires on a hand-written bad example.

    Without this, the lint could regress to "always returns []" without
    any real test catching it.
    """
    bad_source = (
        "from codeprobe.core.scoring import ScoreResult\n"
        "def scorer():\n"
        "    return ScoreResult(score=1.0, passed=True)\n"
    )
    findings = find_missing_scorer_family(bad_source, "synthetic.py")
    assert any(f.rule == "missing-scorer-family" for f in findings), (
        f"Expected missing-scorer-family finding, got: {findings}"
    )


def test_lint_passes_when_scorer_family_declared_in_synthetic_source() -> None:
    """Smoke test — the rule does NOT fire on an honest example."""
    good_source = (
        "from codeprobe.core.scoring import ScoreResult\n"
        "def scorer():\n"
        "    return ScoreResult(score=1.0, passed=True, scorer_family='ok')\n"
    )
    findings = find_missing_scorer_family(good_source, "synthetic.py")
    assert findings == [], f"Expected no findings, got: {findings}"


def test_lint_catches_quiet_recall_fallback_in_synthetic_source() -> None:
    """Smoke test — the F1-family-with-recall-fallback shape is detected."""
    bad_source = (
        "def derive(family, recall, weighted_recall, fallback):\n"
        "    if family == 'oracle_weighted_f1':\n"
        "        if weighted_recall is not None:\n"
        "            reward = weighted_recall\n"
        "        elif recall is not None:\n"
        "            reward = recall\n"
        "        else:\n"
        "            reward = fallback\n"
        "    return reward\n"
    )
    findings = find_quiet_recall_fallbacks(bad_source, "synthetic.py")
    assert len(findings) >= 2, (
        f"Expected at least 2 quiet-recall findings, got: {findings}"
    )


def test_lint_catches_bare_except_in_synthetic_source() -> None:
    """Smoke test — bare ``except:`` and ``except Exception:`` fire."""
    bad_source = (
        "def f():\n"
        "    try:\n"
        "        risky()\n"
        "    except:\n"
        "        return 0.0\n"
        "def g():\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:\n"
        "        return 0.0\n"
    )
    findings = find_bare_excepts(bad_source, "synthetic.py")
    assert len(findings) == 2, (
        f"Expected 2 bare-except findings, got: {findings}"
    )


def test_lint_accepts_noqa_annotated_except_exception() -> None:
    """``except Exception:  # noqa: ...`` is allowed."""
    good_source = (
        "def f():\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:  # noqa: BLE001 — load-bearing\n"
        "        return 0.0\n"
    )
    findings = find_bare_excepts(good_source, "synthetic.py")
    assert findings == [], f"Expected no findings, got: {findings}"


def test_scorer_family_registry_is_non_empty() -> None:
    """Sanity: ``SCORER_FAMILIES`` is the source of valid family names.

    A future scorer that declares a family not in this set is allowed by
    design (the registry is advisory; new per-task rubrics flow through
    before being registered). But the registry itself must contain the
    canonical families so reviewers have something to compare against.
    """
    canonical = {
        "binary_test",
        "continuous",
        "oracle_overlap_f1",
        "oracle_overlap_recall",
        "weighted_checkpoints",
    }
    missing = canonical - set(SCORER_FAMILIES)
    assert not missing, (
        f"SCORER_FAMILIES is missing canonical families: {sorted(missing)}"
    )
