"""Regression tests for module-dependency probe ground truth."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from codeprobe.probe import generator
from codeprobe.probe.dependency import (
    check_module_dependency,
    discover_module_dependencies,
    scan_module_dependencies,
    select_balanced_dependency_pairs,
)


def _write_python_dependency(repo: Path, module_a: str, module_b: str) -> None:
    path_a = repo / module_a
    path_b = repo / module_b
    path_a.mkdir(parents=True)
    path_b.mkdir(parents=True)
    (path_a / "service.py").write_text(
        f"from {Path(module_b).name}.api import handle\n",
        encoding="utf-8",
    )
    (path_b / "api.py").write_text(
        "def handle() -> None:\n    pass\n",
        encoding="utf-8",
    )


def _pin_random(monkeypatch: pytest.MonkeyPatch) -> None:
    rng = random.Random(42)
    monkeypatch.setattr("codeprobe.probe.dependency.random.sample", rng.sample)
    monkeypatch.setattr("codeprobe.probe.dependency.random.shuffle", rng.shuffle)


def test_checks_imports_in_module_source_files(tmp_path: Path) -> None:
    _write_python_dependency(tmp_path, "a", "b")

    assert check_module_dependency(tmp_path, "a", "b") is True
    assert check_module_dependency(tmp_path, "b", "a") is False


def test_generated_family_contains_both_answer_polarities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_python_dependency(tmp_path, "a", "b")
    symbols = [
        generator.Symbol(
            name="use_b",
            kind="function",
            file_path="a/service.py",
            line=1,
        ),
        generator.Symbol(
            name="handle",
            kind="function",
            file_path="b/api.py",
            line=1,
        ),
    ]
    _pin_random(monkeypatch)

    probes = generator._generate_module_dependency_probes(
        symbols,
        generator.BUILTIN_TEMPLATES,
        tmp_path,
        count=2,
    )

    assert {probe.answer for probe in probes} == {"yes", "no"}


def test_sparse_dependency_family_is_balanced_not_padded_with_no(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbols: list[generator.Symbol] = []
    for name in ("a", "b", "c", "d", "e", "f"):
        module = tmp_path / name
        module.mkdir()
        source = (
            "from b.api import handle\n\ndef use() -> None:\n    pass\n"
            if name == "a"
            else "def use() -> None:\n    pass\n"
        )
        (module / "service.py").write_text(source, encoding="utf-8")
        symbols.append(
            generator.Symbol(
                name=f"use_{name}",
                kind="function",
                file_path=f"{name}/service.py",
                line=1,
            )
        )
    _pin_random(monkeypatch)

    probes = generator._generate_module_dependency_probes(
        symbols,
        generator.BUILTIN_TEMPLATES,
        tmp_path,
        count=6,
    )

    assert [probe.answer for probe in probes].count("yes") == 1
    assert [probe.answer for probe in probes].count("no") == 1


def test_handles_python_src_layout_import_names(tmp_path: Path) -> None:
    package = tmp_path / "src" / "app"
    module_a = package / "a"
    module_b = package / "b"
    module_a.mkdir(parents=True)
    module_b.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (module_a / "__init__.py").write_text("", encoding="utf-8")
    (module_a / "service.py").write_text(
        "from app.b.api import handle\n",
        encoding="utf-8",
    )
    (module_b / "api.py").write_text(
        "def handle() -> None:\n    pass\n",
        encoding="utf-8",
    )

    assert check_module_dependency(tmp_path, "src/app/a", "src/app/b") is True


def test_handles_typescript_relative_imports(tmp_path: Path) -> None:
    module_a = tmp_path / "src" / "a"
    module_b = tmp_path / "src" / "b"
    module_a.mkdir(parents=True)
    module_b.mkdir()
    (module_a / "service.ts").write_text(
        'import { handle } from "../b/api";\n',
        encoding="utf-8",
    )
    (module_b / "api.ts").write_text(
        "export function handle(): void {}\n",
        encoding="utf-8",
    )

    assert check_module_dependency(tmp_path, "src/a", "src/b") is True


def test_duplicate_package_names_are_scoped_to_source_root(tmp_path: Path) -> None:
    modules: list[str] = []
    for source_root in ("primary/src", "mirror/src"):
        package = tmp_path / source_root / "app"
        module_a = package / "a"
        module_b = package / "b"
        module_a.mkdir(parents=True)
        module_b.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (module_a / "service.py").write_text(
            "from app.b.api import handle\n",
            encoding="utf-8",
        )
        (module_b / "api.py").write_text(
            "def handle() -> None:\n    pass\n",
            encoding="utf-8",
        )
        modules.extend((f"{source_root}/app/a", f"{source_root}/app/b"))

    dependencies = discover_module_dependencies(tmp_path, modules)

    assert dependencies == {
        ("primary/src/app/a", "primary/src/app/b"),
        ("mirror/src/app/a", "mirror/src/app/b"),
    }


def test_refuses_module_paths_outside_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_python_dependency(tmp_path, "outside", "repo/b")

    assert check_module_dependency(repo, "../outside", "b") is False


def test_refuses_symlinked_module_outside_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    _write_python_dependency(tmp_path, "outside", "repo/b")
    (repo / "a").symlink_to(outside, target_is_directory=True)

    assert check_module_dependency(repo, "a", "b") is False


def test_malformed_module_is_not_treated_as_known_negative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
    (tmp_path / "a" / "broken.py").write_text(
        "def broken(:\n",
        encoding="utf-8",
    )
    (tmp_path / "b" / "api.py").write_text(
        "def handle() -> None:\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "c" / "service.py").write_text(
        "from b.api import handle\n",
        encoding="utf-8",
    )
    modules = ["a", "b", "c"]

    evidence = scan_module_dependencies(tmp_path, modules)
    _pin_random(monkeypatch)
    selected = select_balanced_dependency_pairs(
        modules,
        evidence.positive_pairs,
        count=2,
        negative_sources=evidence.complete_sources,
    )

    assert "a" not in evidence.complete_sources
    assert ("c", "b") in evidence.positive_pairs
    assert all(pair[0] != "a" for pair, answer in selected if answer == "no")
