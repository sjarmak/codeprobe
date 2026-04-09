"""Tests for the comprehension-task generator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeprobe.mining.comprehension import (
    ComprehensionGenerator,
    _answer_files_beat_grep,
    _build_index,
    _reachable_modules,
    _shortest_path_length,
    _single_grep_importers,
    _transitive_importers,
    write_comprehension_tasks,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def chain_repo(tmp_path: Path) -> Path:
    """Tiny repo with a deterministic import chain.

    Layout::

        pkg/
          __init__.py
          a.py      # imports b
          b.py      # imports c
          c.py      # defines foo() -> int, Bar class with baz() method
          d.py      # imports a, calls bar.baz()
          e.py      # unrelated, imports nothing internal
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text(
        "from pkg import b\n" "\n" "def use_a():\n" "    return b.foo_b()\n"
    )
    (pkg / "b.py").write_text(
        "from pkg import c\n" "\n" "def foo_b():\n" "    return c.foo()\n"
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
    (pkg / "e.py").write_text("def standalone() -> bool:\n" "    return True\n")
    return tmp_path


@pytest.fixture
def cross_file_repo(tmp_path: Path) -> Path:
    """Repo where a method calls a cross-file typed function."""
    pkg = tmp_path / "proj"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "helpers.py").write_text(
        "def compute(x: int) -> dict:\n" "    return {'x': x}\n"
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


# ---------------------------------------------------------------------------
# Import graph tests
# ---------------------------------------------------------------------------


def test_build_import_graph_resolves_internal_modules(chain_repo: Path) -> None:
    index = _build_index(chain_repo)
    # All five modules should be registered
    expected = {"pkg", "pkg.a", "pkg.b", "pkg.c", "pkg.d", "pkg.e"}
    assert expected.issubset(index.module_to_file.keys())
    # pkg.a -> pkg.b
    assert "pkg.b" in index.graph["pkg.a"]
    # pkg.b -> pkg.c
    assert "pkg.c" in index.graph["pkg.b"]
    # pkg.e imports nothing internal
    assert index.graph["pkg.e"] == set()


def test_transitive_importers_walks_chain(chain_repo: Path) -> None:
    index = _build_index(chain_repo)
    # pkg.c is imported by pkg.b directly, and by pkg.a/pkg.d transitively
    importers = _transitive_importers(index.rgraph, "pkg.c")
    assert "pkg.b" in importers
    assert "pkg.a" in importers  # a -> b -> c
    assert "pkg.d" in importers  # d -> a -> b -> c (and d -> c directly)
    assert "pkg.e" not in importers


def test_reachable_modules_and_shortest_path(chain_repo: Path) -> None:
    index = _build_index(chain_repo)
    reachable = _reachable_modules(index.graph, "pkg.a")
    assert "pkg.b" in reachable
    assert "pkg.c" in reachable  # reached via pkg.b
    assert "pkg.e" not in reachable
    assert _shortest_path_length(index.graph, "pkg.a", "pkg.c") == 2
    assert _shortest_path_length(index.graph, "pkg.a", "pkg.e") is None


# ---------------------------------------------------------------------------
# Discrimination gate tests
# ---------------------------------------------------------------------------


def test_discrimination_gate_detects_transitive(chain_repo: Path) -> None:
    index = _build_index(chain_repo)
    # Files that directly mention "import pkg.c" or "from pkg.c"
    grep_hits = _single_grep_importers(index, "pkg.c")
    # pkg.b directly imports pkg.c, pkg.d imports "from pkg.c import Bar"
    assert str(Path("pkg/b.py")) in grep_hits
    assert str(Path("pkg/d.py")) in grep_hits
    # pkg.a does NOT contain any `import pkg.c` — only reaches it transitively
    assert str(Path("pkg/a.py")) not in grep_hits

    transitive = _transitive_importers(index.rgraph, "pkg.c")
    answer_files = {
        index.module_to_file[m] for m in transitive if m in index.module_to_file
    }
    # Gate must pass: transitive set contains pkg/a.py which grep misses
    assert _answer_files_beat_grep(index, "pkg.c", answer_files) is True


def test_discrimination_gate_rejects_grep_equivalent(tmp_path: Path) -> None:
    """Flat repo where every importer is direct — gate should reject."""
    pkg = tmp_path / "flat"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text("def f() -> int:\n    return 1\n")
    (pkg / "user_a.py").write_text("from flat import core\n")
    (pkg / "user_b.py").write_text("from flat import core\n")
    index = _build_index(tmp_path)

    transitive = _transitive_importers(index.rgraph, "flat.core")
    answer_files = {
        index.module_to_file[m] for m in transitive if m in index.module_to_file
    }
    # All importers are direct, so grep finds them all — gate fails.
    assert _answer_files_beat_grep(index, "flat.core", answer_files) is False


# ---------------------------------------------------------------------------
# Generator behaviour
# ---------------------------------------------------------------------------


def test_generate_returns_comprehension_tasks(chain_repo: Path) -> None:
    gen = ComprehensionGenerator(chain_repo)
    tasks = gen.generate(count=8)
    assert len(tasks) >= 1
    for task in tasks:
        assert task.metadata.task_type == "architecture_comprehension"
        assert task.verification.verification_mode == "artifact_eval"
        assert task.verification.ground_truth_path == "tests/ground_truth.json"
        assert "comprehension" in task.metadata.tags


def test_generate_covers_multiple_templates(chain_repo: Path) -> None:
    gen = ComprehensionGenerator(chain_repo)
    tasks = gen.generate(count=12)
    templates = {
        tag
        for task in tasks
        for tag in task.metadata.tags
        if tag
        in {
            "import_chain",
            "dependency_analysis",
            "return_type_resolution",
            "transitive_dependency",
        }
    }
    # At minimum we must see import_chain and transitive_dependency
    assert "import_chain" in templates
    assert "transitive_dependency" in templates


def test_transitive_dependency_produces_both_polarities(chain_repo: Path) -> None:
    from codeprobe.mining.comprehension import _TASK_SPECS

    gen = ComprehensionGenerator(chain_repo)
    tasks = gen.generate(count=12)
    bool_answers = []
    for task in tasks:
        spec = _TASK_SPECS.get(task.id)
        if spec and spec.template == "transitive_dependency":
            bool_answers.append(spec.answer)
    assert True in bool_answers
    assert False in bool_answers


def test_return_type_resolution_uses_real_annotation(cross_file_repo: Path) -> None:
    from codeprobe.mining.comprehension import _TASK_SPECS

    gen = ComprehensionGenerator(cross_file_repo)
    tasks = gen.generate(count=4)
    rt_specs = [
        _TASK_SPECS[t.id]
        for t in tasks
        if _TASK_SPECS.get(t.id)
        and _TASK_SPECS[t.id].template == "return_type_resolution"
    ]
    assert rt_specs, "expected at least one return_type_resolution task"
    spec = rt_specs[0]
    assert spec.answer_type == "text"
    # helpers.compute -> dict; the answer should be exactly "dict"
    assert spec.answer == "dict"


def test_import_chain_answer_contains_only_reachable_files(chain_repo: Path) -> None:
    from codeprobe.mining.comprehension import _TASK_SPECS

    gen = ComprehensionGenerator(chain_repo)
    tasks = gen.generate(count=8)
    ic_specs = [
        _TASK_SPECS[t.id]
        for t in tasks
        if _TASK_SPECS.get(t.id) and _TASK_SPECS[t.id].template == "import_chain"
    ]
    assert ic_specs
    spec = ic_specs[0]
    assert spec.answer_type == "file_list"
    assert isinstance(spec.answer, list)
    # Files should be relative and sorted
    assert spec.answer == sorted(spec.answer)
    # Should not include the target module's own file
    target_file = str(Path("pkg") / (spec.target.split(".")[-1] + ".py"))
    # The target module may be pkg.c (file pkg/c.py) — own file excluded
    if spec.target == "pkg.c":
        assert target_file not in spec.answer


# ---------------------------------------------------------------------------
# Self-repo smoke test
# ---------------------------------------------------------------------------


def test_generate_on_self_src_produces_multiple_tasks() -> None:
    src = Path(__file__).resolve().parent.parent / "src" / "codeprobe"
    assert src.is_dir(), f"codeprobe src not found at {src}"
    gen = ComprehensionGenerator(src)
    tasks = gen.generate(count=10)
    assert len(tasks) >= 3, f"expected >= 3 tasks on self src, got {len(tasks)}"


# ---------------------------------------------------------------------------
# Writer tests
# ---------------------------------------------------------------------------


def test_write_comprehension_tasks_creates_files(
    chain_repo: Path, tmp_path: Path
) -> None:
    gen = ComprehensionGenerator(chain_repo)
    tasks = gen.generate(count=4)
    assert tasks, "need at least one task to write"

    out = tmp_path / "out"
    written = write_comprehension_tasks(tasks, out)
    assert written
    for task_dir in written:
        assert (task_dir / "instruction.md").is_file()
        assert (task_dir / "metadata.json").is_file()
        gt_path = task_dir / "tests" / "ground_truth.json"
        assert gt_path.is_file()
        data = json.loads(gt_path.read_text())
        # The 4 required keys
        assert set(data.keys()) == {
            "answer",
            "answer_type",
            "confidence",
            "provenance",
        }


def test_ground_truth_provenance_is_deterministic(
    chain_repo: Path, tmp_path: Path
) -> None:
    gen = ComprehensionGenerator(chain_repo)
    tasks = gen.generate(count=4)
    out = tmp_path / "out"
    written = write_comprehension_tasks(tasks, out)
    assert written
    for task_dir in written:
        gt = json.loads((task_dir / "tests" / "ground_truth.json").read_text())
        assert gt["provenance"] == "deterministic"
        assert gt["confidence"] == 0.95
        assert gt["answer_type"] in {"file_list", "boolean", "text", "count"}


def test_instruction_md_contains_question(chain_repo: Path, tmp_path: Path) -> None:
    gen = ComprehensionGenerator(chain_repo)
    tasks = gen.generate(count=4)
    out = tmp_path / "out"
    written = write_comprehension_tasks(tasks, out)
    assert written
    instruction_text = (written[0] / "instruction.md").read_text()
    assert "## Question" in instruction_text
    assert "## Answer Format" in instruction_text
    assert "architecture_comprehension" in instruction_text
