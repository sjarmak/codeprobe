"""Config-driven tool-surface policy lint (codeprobe-1gg, ZFC compliance).

The tool-surface audit flags arms where the agent abandoned an enabled tool
surface. For that signal to be honest, the surface vocabulary must come from
the experiment config — NOT from literals baked into source. A hardcoded
``ToolSurfacePolicy(surface="sourcegraph", prefixes=("sg_",))`` would be
exactly the semantic-judgment-in-code that ZFC forbids: it encodes "these
are the surfaces that matter" as policy hidden in the analyzer, and it
silently goes stale when an experiment declares a different server.

This lint is an AST scan over ``src/codeprobe/`` for any
:class:`~codeprobe.core.tool_surface_audit.ToolSurfacePolicy` constructor
that receives a string LITERAL for its ``surface`` name or for any entry of
its ``prefixes`` tuple. Derivations from config values (variables,
f-strings such as ``f"mcp__{name}"``) are allowed; raw string constants are
flagged. Tests are exempt — fixtures legitimately hardcode surfaces.

Mirrors the structure of ``test_scorer_honesty.py``: a pytest test so it
runs in the default suite, with a tracked (currently empty) allowlist.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = REPO_ROOT / "src" / "codeprobe"

_POLICY_CTOR = "ToolSurfacePolicy"


@dataclass(frozen=True)
class Offender:
    relpath: str
    line: int
    reason: str
    follow_up_bead: str


# No known offenders — the analyzer derives every policy from config. When
# adding an entry here, a reviewer must sign off and the offender must carry
# a follow-up bead. Delete the entry when the offender is fixed.
_KNOWN_OFFENDERS: tuple[Offender, ...] = ()


@dataclass(frozen=True)
class Finding:
    relpath: str
    line: int
    detail: str

    def format(self) -> str:
        return f"{self.relpath}:{self.line} [hardcoded-surface] {self.detail}"


def _relpath(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _is_policy_ctor(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == _POLICY_CTOR
    if isinstance(func, ast.Attribute):
        return func.attr == _POLICY_CTOR
    return False


def _str_literal(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _arg(call: ast.Call, name: str, position: int) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    if len(call.args) > position:
        return call.args[position]
    return None


def _check_call(call: ast.Call, relpath: str) -> list[Finding]:
    findings: list[Finding] = []
    surface = _arg(call, "surface", 0)
    if surface is not None and _str_literal(surface):
        findings.append(
            Finding(
                relpath,
                surface.lineno,
                "surface name is a string literal; derive it from "
                "ExperimentConfig (mcp_config / allowed_tools).",
            )
        )
    prefixes = _arg(call, "prefixes", 1)
    if isinstance(prefixes, (ast.Tuple, ast.List)):
        for elt in prefixes.elts:
            if _str_literal(elt):
                findings.append(
                    Finding(
                        relpath,
                        elt.lineno,
                        "prefix is a string literal; derive prefixes from "
                        "declared server names, not hardcoded surfaces.",
                    )
                )
    return findings


def _scan_file(path: Path) -> list[Finding]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    relpath = _relpath(path)
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_policy_ctor(node):
            findings.extend(_check_call(node, relpath))
    return findings


def _is_known(f: Finding) -> bool:
    return any(
        o.relpath == f.relpath and o.line == f.line for o in _KNOWN_OFFENDERS
    )


def test_tool_surface_policies_are_config_driven() -> None:
    findings: list[Finding] = []
    for path in sorted(_SRC.rglob("*.py")):
        findings.extend(_scan_file(path))

    unexpected = [f for f in findings if not _is_known(f)]
    assert not unexpected, (
        "Hardcoded ToolSurfacePolicy literals found — surfaces must be "
        "derived from ExperimentConfig (ZFC):\n"
        + "\n".join(f.format() for f in unexpected)
    )


def test_no_stale_known_offenders() -> None:
    """Every allowlist entry must still correspond to a live finding."""
    findings = {
        (f.relpath, f.line)
        for path in sorted(_SRC.rglob("*.py"))
        for f in _scan_file(path)
    }
    stale = [o for o in _KNOWN_OFFENDERS if (o.relpath, o.line) not in findings]
    assert not stale, (
        "Stale _KNOWN_OFFENDERS entries (offender fixed — delete them):\n"
        + "\n".join(f"{o.relpath}:{o.line} {o.reason}" for o in stale)
    )
