"""Tests for comprehension consensus — the independent AST second backend.

The comprehension generator derives answers from a regex-built import graph
(``codeprobe.mining._graph``). ``comprehension_consensus`` re-derives every
answer from a real :mod:`ast` parse and only ships tasks where both backends
agree exactly. Disagreement quarantines the task — that is the mechanism
working, never a failure to be papered over.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from codeprobe.mining.comprehension import (
    ComprehensionGenerator,
    ComprehensionTaskSpec,
    write_comprehension_tasks,
)
from codeprobe.mining.comprehension_consensus import (
    AST_BACKEND,
    GENERATOR_BACKEND,
    build_ast_index,
    mine_time_commit,
    rederive_answer,
    verify_comprehension_tasks,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def chain_repo(tmp_path: Path) -> Path:
    """Clean repo where regex and AST derivations must agree.

    Layout::

        pkg/
          __init__.py
          a.py      # imports b
          b.py      # imports c
          c.py      # defines foo() -> int, Bar class with baz() method
          d.py      # imports a, calls Bar().baz()
          e.py      # unrelated
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text(
        "from pkg import b\n\ndef use_a():\n    return b.foo_b()\n"
    )
    (pkg / "b.py").write_text(
        "from pkg import c\n\ndef foo_b():\n    return c.foo()\n"
    )
    (pkg / "c.py").write_text(
        "def foo() -> int:\n"
        "    return 42\n"
        "\n"
        "\n"
        "class Bar:\n"
        "    def baz(self) -> str:\n"
        "        return 'hi'\n"
    )
    (pkg / "d.py").write_text(
        "from pkg import a\n"
        "from pkg.c import Bar\n"
        "\n"
        "def runner():\n"
        "    a.use_a()\n"
        "    Bar().baz()\n"
        "    return True\n"
    )
    (pkg / "e.py").write_text("def standalone() -> bool:\n    return True\n")
    return tmp_path


@pytest.fixture
def string_import_repo(tmp_path: Path) -> Path:
    """Repo where a docstring contains an import statement.

    The regex backend sees ``fake.py`` as an importer of ``pkg.target``
    (its multiline regex matches inside the docstring); the AST backend
    does not. import_chain answers for ``pkg.target`` therefore diverge.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "target.py").write_text("VALUE = 1\n")
    (pkg / "mid.py").write_text("from pkg import target\n\nX = target.VALUE\n")
    (pkg / "top.py").write_text("from pkg import mid\n\nY = mid.X\n")
    (pkg / "fake.py").write_text(
        '"""Docs.\n\nimport pkg.target\n"""\n\nZ = 3\n'
    )
    return tmp_path


@pytest.fixture
def cross_file_repo(tmp_path: Path) -> Path:
    """Repo where a method calls exactly one cross-file typed function."""
    pkg = tmp_path / "proj"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "helpers.py").write_text(
        "def compute(x: int) -> dict:\n    return {'x': x}\n"
    )
    (pkg / "service.py").write_text(
        "from proj.helpers import compute\n"
        "\n"
        "\n"
        "class Service:\n"
        "    def run(self, n: int) -> dict:\n"
        "        result = compute(n)\n"
        "        return result\n"
    )
    return tmp_path


@pytest.fixture
def ambiguous_return_repo(tmp_path: Path) -> Path:
    """Method calls TWO cross-file typed functions with different
    annotations — the return_type question has no well-defined answer."""
    pkg = tmp_path / "proj"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "helpers.py").write_text(
        "def compute(x: int) -> dict:\n    return {'x': x}\n"
    )
    (pkg / "util.py").write_text(
        "def fetch(x: int) -> list:\n    return [x]\n"
    )
    (pkg / "service.py").write_text(
        "from proj.helpers import compute\n"
        "from proj.util import fetch\n"
        "\n"
        "\n"
        "class Service:\n"
        "    def run(self, n: int) -> dict:\n"
        "        a = compute(n)\n"
        "        b = fetch(n)\n"
        "        return a\n"
    )
    return tmp_path


def _specs_by_id(gen: ComprehensionGenerator, tasks: list) -> dict:
    from codeprobe.mining.comprehension import _TASK_SPECS

    return {t.id: _TASK_SPECS[t.id] for t in tasks}


# ---------------------------------------------------------------------------
# AST index
# ---------------------------------------------------------------------------


def test_ast_index_builds_import_edges(chain_repo: Path) -> None:
    index = build_ast_index(chain_repo)
    assert "pkg.b" in index.graph["pkg.a"]
    assert "pkg.c" in index.graph["pkg.b"]
    assert "pkg.a" in index.graph["pkg.d"]
    assert "pkg.c" in index.graph["pkg.d"]  # from pkg.c import Bar
    assert index.graph["pkg.e"] == set()
    # reverse edges mirror forward edges
    assert "pkg.a" in index.rgraph["pkg.b"]


def test_ast_index_ignores_imports_inside_strings(
    string_import_repo: Path,
) -> None:
    index = build_ast_index(string_import_repo)
    assert "pkg.target" not in index.graph["pkg.fake"]


def test_ast_index_resolves_relative_imports(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from . import leaf\n")
    (pkg / "leaf.py").write_text("VALUE = 1\n")
    (pkg / "sibling.py").write_text("from .leaf import VALUE\n\nX = VALUE\n")
    index = build_ast_index(tmp_path)
    assert "pkg.leaf" in index.graph["pkg"]
    assert "pkg.leaf" in index.graph["pkg.sibling"]


# ---------------------------------------------------------------------------
# Per-template rederivation
# ---------------------------------------------------------------------------


def test_rederive_import_chain_matches_generator(chain_repo: Path) -> None:
    gen = ComprehensionGenerator(chain_repo)
    tasks = gen.generate(count=8)
    specs = _specs_by_id(gen, tasks)
    chain_specs = [s for s in specs.values() if s.template == "import_chain"]
    assert chain_specs, "fixture must yield an import_chain task"
    index = build_ast_index(chain_repo)
    for spec in chain_specs:
        result = rederive_answer(spec, index)
        assert result.answer == spec.answer


def test_rederive_transitive_dependency_matches_generator(
    chain_repo: Path,
) -> None:
    gen = ComprehensionGenerator(chain_repo)
    tasks = gen.generate(count=8)
    specs = _specs_by_id(gen, tasks)
    trans = [
        s for s in specs.values() if s.template == "transitive_dependency"
    ]
    assert trans, "fixture must yield a transitive_dependency task"
    index = build_ast_index(chain_repo)
    for spec in trans:
        result = rederive_answer(spec, index)
        assert result.answer == spec.answer


def test_rederive_return_type_matches_generator(cross_file_repo: Path) -> None:
    gen = ComprehensionGenerator(cross_file_repo)
    tasks = gen.generate(count=8)
    specs = _specs_by_id(gen, tasks)
    rtypes = [
        s for s in specs.values() if s.template == "return_type_resolution"
    ]
    assert rtypes, "fixture must yield a return_type_resolution task"
    index = build_ast_index(cross_file_repo)
    for spec in rtypes:
        result = rederive_answer(spec, index)
        assert result.answer == spec.answer == "dict"


def test_rederive_return_type_flags_ambiguity(
    ambiguous_return_repo: Path,
) -> None:
    gen = ComprehensionGenerator(ambiguous_return_repo)
    tasks = gen.generate(count=8)
    specs = _specs_by_id(gen, tasks)
    rtypes = [
        s for s in specs.values() if s.template == "return_type_resolution"
    ]
    assert rtypes, "fixture must yield a return_type_resolution task"
    index = build_ast_index(ambiguous_return_repo)
    result = rederive_answer(rtypes[0], index)
    # Two cross-file typed callees with different annotations: the AST
    # backend refuses to pick one, so the answers cannot agree.
    assert result.answer is None
    assert "ambiguous" in (result.detail or "")


def test_rederive_dependency_analysis_ignores_comment_calls(
    tmp_path: Path,
) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "m.py").write_text("def foo(x):\n    return x\n")
    (pkg / "caller.py").write_text("from pkg import m\n\nR = m.foo(1)\n")
    (pkg / "fake.py").write_text(
        "from pkg import m\n\n# call foo() someday\nS = 2\n"
    )
    (pkg / "noise.py").write_text("def unrelated():\n    return foo(1)\n")

    gen = ComprehensionGenerator(tmp_path)
    tasks = gen.generate(count=8)
    specs = _specs_by_id(gen, tasks)
    deps = [
        s
        for s in specs.values()
        if s.template == "dependency_analysis" and s.target == "pkg.m.foo"
    ]
    assert deps, "fixture must yield the pkg.m.foo dependency task"
    spec = deps[0]
    # The regex backend counted the comment-only file as a caller...
    assert "pkg/fake.py" in spec.answer
    # ...the AST backend must not.
    index = build_ast_index(tmp_path)
    result = rederive_answer(spec, index)
    assert result.answer == ["pkg/caller.py"]


# ---------------------------------------------------------------------------
# Consensus verification
# ---------------------------------------------------------------------------


def test_verify_ships_agreeing_tasks_with_two_backends(
    chain_repo: Path,
) -> None:
    gen = ComprehensionGenerator(chain_repo)
    tasks = gen.generate(count=8)
    specs = _specs_by_id(gen, tasks)
    consensus = verify_comprehension_tasks(chain_repo, tasks, specs)

    assert set(consensus) == {t.id for t in tasks}
    agreed = [tc for tc in consensus.values() if tc.agreed]
    assert agreed, "clean fixture must ship at least one task"
    for tc in agreed:
        report = tc.report
        assert report["schema_version"] == "consensus.v1"
        assert report["decision"] == "shipped"
        backends = [b["backend"] for b in report["backend_results"]]
        assert backends == [GENERATOR_BACKEND, AST_BACKEND]
        for b in report["backend_results"]:
            assert b["available"] is True
            assert b["error"] is None


def test_verify_quarantines_divergent_import_chain(
    string_import_repo: Path,
) -> None:
    gen = ComprehensionGenerator(string_import_repo)
    tasks = gen.generate(count=8)
    specs = _specs_by_id(gen, tasks)
    chain_ids = [
        t.id
        for t in tasks
        if specs[t.id].template == "import_chain"
        and specs[t.id].target == "pkg.target"
    ]
    assert chain_ids, "fixture must yield the pkg.target import_chain task"
    consensus = verify_comprehension_tasks(string_import_repo, tasks, specs)
    tc = consensus[chain_ids[0]]
    assert tc.agreed is False
    assert tc.report["decision"] == "quarantined"
    assert tc.reason
    # Both backends recorded their own answers so a reviewer can triage.
    by_name = {b["backend"]: b for b in tc.report["backend_results"]}
    assert "pkg/fake.py" in by_name[GENERATOR_BACKEND]["files"]
    assert "pkg/fake.py" not in by_name[AST_BACKEND]["files"]


def test_verify_file_lists_populate_backend_files(chain_repo: Path) -> None:
    gen = ComprehensionGenerator(chain_repo)
    tasks = gen.generate(count=8)
    specs = _specs_by_id(gen, tasks)
    consensus = verify_comprehension_tasks(chain_repo, tasks, specs)
    for task in tasks:
        spec = specs[task.id]
        tc = consensus[task.id]
        by_name = {b["backend"]: b for b in tc.report["backend_results"]}
        if spec.answer_type == "file_list" and tc.agreed:
            assert by_name[GENERATOR_BACKEND]["files"] == sorted(spec.answer)
            assert by_name[AST_BACKEND]["files"] == sorted(spec.answer)
            assert tc.report["consensus_files"] == sorted(spec.answer)
        else:
            # Scalar answers carry no file-set; the answer field does.
            assert by_name[GENERATOR_BACKEND]["answer"] == spec.answer


# ---------------------------------------------------------------------------
# mine-time commit
# ---------------------------------------------------------------------------


def test_mine_time_commit_reads_head(tmp_path: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", str(tmp_path)], check=True, capture_output=True
    )
    (tmp_path / "f.py").write_text("X = 1\n")
    env_args = [
        "-c", "user.email=t@t", "-c", "user.name=t",
    ]
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "."],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), *env_args, "commit", "-q", "-m", "init"],
        check=True, capture_output=True,
    )
    head = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert mine_time_commit(tmp_path) == head


def test_mine_time_commit_returns_none_outside_git(tmp_path: Path) -> None:
    assert mine_time_commit(tmp_path) is None


# ---------------------------------------------------------------------------
# Writer integration — commit + divergence_report.json
# ---------------------------------------------------------------------------


def test_writer_records_commit_and_divergence_report(
    chain_repo: Path, tmp_path: Path
) -> None:
    gen = ComprehensionGenerator(chain_repo)
    tasks = gen.generate(count=8, dual=True)
    specs = _specs_by_id(gen, tasks)
    consensus = verify_comprehension_tasks(chain_repo, tasks, specs)
    shipped = [t for t in tasks if consensus[t.id].agreed]
    assert shipped
    reports = {t.id: consensus[t.id].report for t in shipped}
    commit = "a3c0ffee1234567890abcdef1234567890abcdef"

    out = tmp_path / "out"
    written = write_comprehension_tasks(
        shipped,
        out,
        repo_path=chain_repo,
        commit=commit,
        divergence_reports=reports,
    )
    assert written
    for task_dir in written:
        gt = json.loads(
            (task_dir / "tests" / "ground_truth.json").read_text()
        )
        assert gt["commit"] == commit
        # ground truth still validates against the artifact scorer schema
        from codeprobe.core.scoring import validate_ground_truth

        assert validate_ground_truth(gt) is None

        report_path = task_dir / "divergence_report.json"
        assert report_path.is_file(), "shipped task needs its consensus record"
        report = json.loads(report_path.read_text())
        # The exact field names the aoa-bench loader deserializes.
        assert report["decision"] == "shipped"
        for b in report["backend_results"]:
            assert set(b) >= {"backend", "available", "files", "error"}
        names = {b["backend"] for b in report["backend_results"]}
        assert len(names) >= 2, "NativeComposed needs >= 2 distinct backends"


def test_writer_without_consensus_keeps_legacy_shape(
    chain_repo: Path, tmp_path: Path
) -> None:
    gen = ComprehensionGenerator(chain_repo)
    tasks = gen.generate(count=4)
    out = tmp_path / "out"
    written = write_comprehension_tasks(tasks, out)
    assert written
    for task_dir in written:
        gt = json.loads(
            (task_dir / "tests" / "ground_truth.json").read_text()
        )
        assert "commit" not in gt
        assert not (task_dir / "divergence_report.json").exists()


# ---------------------------------------------------------------------------
# Mine dispatch — consensus gating end to end
# ---------------------------------------------------------------------------


def _git_init_commit(repo: Path) -> str:
    subprocess.run(
        ["git", "init", "-q", str(repo)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        [
            "git", "-C", str(repo), "-c", "user.email=t@t",
            "-c", "user.name=t", "commit", "-q", "-m", "init",
        ],
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_dispatch_writes_consensus_artifacts_and_commit(
    chain_repo: Path,
) -> None:
    from codeprobe.cli import mine_cmd

    head = _git_init_commit(chain_repo)
    mine_cmd._dispatch_comprehension(
        repo_path=chain_repo,
        count=8,
        goal_name="navigation",
        bias="",
        dual_verify=True,
    )
    tasks_dir = chain_repo / ".codeprobe" / "tasks"
    task_dirs = sorted(p for p in tasks_dir.iterdir() if p.is_dir())
    assert task_dirs, "clean fixture must ship tasks"
    for td in task_dirs:
        report = json.loads((td / "divergence_report.json").read_text())
        assert report["decision"] == "shipped"
        assert {b["backend"] for b in report["backend_results"]} == {
            GENERATOR_BACKEND,
            AST_BACKEND,
        }
        gt = json.loads((td / "tests" / "ground_truth.json").read_text())
        assert gt["commit"] == head
        # The executor-pinning field must stay empty (unpinned tasks run
        # at HEAD; the expB anchor rewrite must never see a commit here).
        meta = json.loads((td / "metadata.json").read_text())
        assert (meta["metadata"].get("ground_truth_commit") or "") == ""

    summary = mine_cmd._COMPREHENSION_CONSENSUS
    assert summary is not None
    assert summary["shipped"] == len(task_dirs)
    assert summary["quarantined"] == 0
    assert summary["commit"] == head


def test_dispatch_quarantines_divergent_tasks(
    string_import_repo: Path,
) -> None:
    from codeprobe.cli import mine_cmd

    _git_init_commit(string_import_repo)
    mine_cmd._dispatch_comprehension(
        repo_path=string_import_repo,
        count=8,
        goal_name="navigation",
        bias="",
        dual_verify=True,
    )
    tasks_dir = string_import_repo / ".codeprobe" / "tasks"
    shipped_ids = (
        {p.name for p in tasks_dir.iterdir() if p.is_dir()}
        if tasks_dir.is_dir()
        else set()
    )
    assert not any("import_chain" in tid for tid in shipped_ids), (
        "the divergent import_chain task must not ship"
    )
    quarantine_dir = string_import_repo / ".codeprobe" / "tasks_quarantined"
    qdirs = sorted(p for p in quarantine_dir.iterdir() if p.is_dir())
    assert qdirs, "quarantined task must be preserved for triage"
    q_report = json.loads(
        (qdirs[0] / "divergence_report.json").read_text()
    )
    assert q_report["decision"] == "quarantined"

    summary = mine_cmd._COMPREHENSION_CONSENSUS
    assert summary is not None
    assert summary["quarantined"] == len(qdirs)
    assert summary["quarantined_tasks"]
    assert summary["quarantined_tasks"][0]["reason"]


# ---------------------------------------------------------------------------
# Unknown template safety
# ---------------------------------------------------------------------------


def test_unknown_template_never_agrees(chain_repo: Path) -> None:
    spec = ComprehensionTaskSpec(
        template="mystery",
        question="?",
        answer="x",
        answer_type="text",
        target="pkg.a",
    )
    index = build_ast_index(chain_repo)
    result = rederive_answer(spec, index)
    assert result.answer is None
    assert result.detail
