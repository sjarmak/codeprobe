"""Oracle fixture registry for the capability test matrix.

Fixtures reference real oracle corpora by path (NOT copies), so the matrix
exercises the same artifacts downstream users and mol-focus-review consume.
When an oracle path is missing (e.g. portable CI checkouts), the individual
cell is skipped with a clear reason rather than hiding the gap.

Per bead codeprobe-l6u acceptance criteria, tests reference oracle corpus
paths under /home/ds/projects/{MCP-Eval-Tasks,CodeScaleBench,EnterpriseBench}/.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Oracle corpus roots (real paths — NOT copied)
# ---------------------------------------------------------------------------

MCP_EVAL_TASKS_ROOT = Path("/home/ds/projects/MCP-Eval-Tasks")
ENTERPRISE_BENCH_ROOT = Path("/home/ds/projects/EnterpriseBench")
CODE_SCALE_BENCH_ROOT = Path("/home/ds/projects/CodeScaleBench")


@dataclass(frozen=True)
class OracleFixture:
    """Pointer to a single oracle task or artifact on disk."""

    name: str
    corpus: str          # e.g. "MCP-Eval-Tasks", "EnterpriseBench"
    language: str        # "go", "python", ...
    task_type: str       # "compliance-audit", "dependency_management", ...
    path: Path           # task directory OR single file for single-artifact cases

    def exists(self) -> bool:
        return self.path.exists()

    def skip_reason(self) -> str:
        return f"oracle fixture {self.corpus}/{self.name} not present at {self.path}"


# ---------------------------------------------------------------------------
# Fast fixtures — kept small enough that each capability test stays well under
# the 30s per-test guideline in bead codeprobe-l6u.
# ---------------------------------------------------------------------------

# MCP-Eval-Tasks — full task directory layouts (task.toml + tests/ + instructions)
MCP_CCX_SGAUTH_301 = OracleFixture(
    name="ccx-sgauth-301",
    corpus="MCP-Eval-Tasks",
    language="go",
    task_type="compliance-audit",
    path=MCP_EVAL_TASKS_ROOT / "ccx-sgauth-301",
)

MCP_SG_DEEPSEARCH_ANCHOR = OracleFixture(
    name="sg-deepsearch-anchor-fix-001",
    corpus="MCP-Eval-Tasks",
    language="go",
    task_type="anchor-fix",
    path=MCP_EVAL_TASKS_ROOT / "sg-deepsearch-anchor-fix-001",
)

# EnterpriseBench — individual task.toml files (single-file oracle shape)
EB_EXAMPLE_TASK = OracleFixture(
    name="EXAMPLE_TASK.toml",
    corpus="EnterpriseBench",
    language="go",
    task_type="dependency_management",
    path=ENTERPRISE_BENCH_ROOT / "benchmarks" / "EXAMPLE_TASK.toml",
)

EB_URLLIB3_REQUESTS = OracleFixture(
    name="dep-mgmt-urllib3-requests-001.toml",
    corpus="EnterpriseBench",
    language="python",
    task_type="dependency_management",
    path=ENTERPRISE_BENCH_ROOT / "benchmarks" / "mined" / "dep-mgmt-urllib3-requests-001.toml",
)


# ---------------------------------------------------------------------------
# Matrix groupings — capability tests parametrize over subsets of these.
# ---------------------------------------------------------------------------

ALL_FIXTURES: tuple[OracleFixture, ...] = (
    MCP_CCX_SGAUTH_301,
    MCP_SG_DEEPSEARCH_ANCHOR,
    EB_EXAMPLE_TASK,
    EB_URLLIB3_REQUESTS,
)

# Oracles with full task directories (instruction.md + task.toml + tests/).
FULL_TASK_FIXTURES: tuple[OracleFixture, ...] = (
    MCP_CCX_SGAUTH_301,
    MCP_SG_DEEPSEARCH_ANCHOR,
)

# Cross-corpus, cross-language coverage — used for matrix-style parametrize.
CROSS_MATRIX: tuple[OracleFixture, ...] = (
    MCP_CCX_SGAUTH_301,       # go, MCP-Eval-Tasks, compliance-audit
    EB_URLLIB3_REQUESTS,      # python, EnterpriseBench, dependency_management
)
