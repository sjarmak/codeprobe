"""Mechanical import-graph discovery for module-dependency probes."""

from __future__ import annotations

import ast
import os
import random
import re
from dataclasses import dataclass
from pathlib import Path

_SOURCE_SUFFIXES = frozenset({".py", ".ts", ".tsx"})
_TS_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx"})
_TS_IMPORT_SPECIFIER_RE = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(\s*|\brequire\s*\(\s*|^\s*import\s*)"""
    r"""['"](?P<specifier>[^'"]+)['"]""",
    re.MULTILINE,
)


@dataclass(frozen=True)
class ModuleDependencyEvidence:
    """Known-positive edges and modules whose outgoing scan was complete."""

    positive_pairs: frozenset[tuple[str, str]]
    complete_sources: frozenset[str]


def check_module_dependency(repo_root: Path, module_a: str, module_b: str) -> bool:
    """Return whether any source file in *module_a* imports *module_b*."""
    return (module_a, module_b) in discover_module_dependencies(
        repo_root,
        sorted({module_a, module_b}),
    )


def _resolve_module_sources(repo_root: Path, module_name: str) -> tuple[Path, ...]:
    try:
        module_path = _resolve_module_location(repo_root, module_name)
    except ValueError:
        return ()
    if module_path.is_dir():
        return tuple(
            sorted(
                path
                for path in module_path.iterdir()
                if path.is_file()
                and path.suffix in _SOURCE_SUFFIXES
                and _is_within_repo(repo_root, path)
            )
        )

    normalized = module_name.replace(".", "/")
    candidates = (
        repo_root / f"{normalized}.py",
        repo_root / f"{normalized}.ts",
        repo_root / f"{normalized}.tsx",
        repo_root / f"{module_name}.py",
        repo_root / f"{module_name}.ts",
    )
    return tuple(
        candidate
        for candidate in candidates
        if candidate.is_file() and _is_within_repo(repo_root, candidate)
    )


def _resolve_module_location(repo_root: Path, module_name: str) -> Path:
    literal_path = repo_root / Path(module_name)
    candidate = (
        literal_path
        if literal_path.exists()
        else repo_root / module_name.replace(".", "/")
    )
    if not _is_within_repo(repo_root, candidate):
        raise ValueError(f"module path escapes repository: {module_name!r}")
    return candidate


def _is_within_repo(repo_root: Path, path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(repo_root.resolve())
    except OSError:
        return False


def _python_package_context(
    repo_root: Path,
    module_name: str,
) -> tuple[str, str]:
    module_path = _resolve_module_location(repo_root, module_name)
    module_path = module_path.parent if module_path.is_file() else module_path

    parts = [module_path.name]
    current = module_path
    while current.parent != repo_root and (current.parent / "__init__.py").is_file():
        current = current.parent
        parts.append(current.name)
    try:
        source_root = current.parent.relative_to(repo_root).as_posix()
    except ValueError:
        source_root = current.parent.as_posix()
    return source_root, ".".join(reversed(parts))


def _python_import_targets(
    content: str,
    *,
    current_package: str,
) -> frozenset[str] | None:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return None

    discovered: set[str] = set()
    for node in ast.walk(tree):
        targets: list[str] = []
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module_parts = node.module.split(".") if node.module else []
            if node.level:
                package_parts = current_package.split(".")
                parents_to_drop = node.level - 1
                if parents_to_drop > len(package_parts):
                    continue
                base_parts = package_parts[: len(package_parts) - parents_to_drop]
                module_parts = [*base_parts, *module_parts]
            module_name = ".".join(module_parts)
            if module_name:
                targets.append(module_name)
            targets.extend(
                ".".join(part for part in (module_name, alias.name) if part)
                for alias in node.names
                if alias.name != "*"
            )
        discovered.update(targets)
    return frozenset(discovered)


def _match_python_module(
    target: str,
    source_root: str,
    lookup: dict[tuple[str, str], set[str]],
) -> str | None:
    candidate = target
    while candidate:
        matches = lookup.get((source_root, candidate))
        if matches is not None:
            return next(iter(matches)) if len(matches) == 1 else None
        candidate = candidate.rpartition(".")[0]
    return None


def _match_typescript_module(
    repo_root: Path,
    source_path: Path,
    specifier: str,
    modules: frozenset[str],
) -> str | None:
    if specifier.startswith("."):
        candidate_path = Path(os.path.normpath(source_path.parent / specifier))
        try:
            candidate = candidate_path.relative_to(repo_root)
        except ValueError:
            return None
    else:
        candidate = Path(specifier)
    if candidate.suffix in _TS_SUFFIXES:
        candidate = candidate.with_suffix("")

    while candidate.parts:
        module_name = candidate.as_posix()
        if module_name in modules:
            return module_name
        candidate = candidate.parent
    return None


def discover_module_dependencies(
    repo_root: Path,
    modules: list[str],
) -> frozenset[tuple[str, str]]:
    """Discover dependency edges in one linear scan of module source files."""
    return scan_module_dependencies(repo_root, modules).positive_pairs


def scan_module_dependencies(
    repo_root: Path,
    modules: list[str],
) -> ModuleDependencyEvidence:
    """Return positive edges plus sources safe to use for negative examples."""
    python_lookup: dict[tuple[str, str], set[str]] = {}
    python_contexts: dict[str, tuple[str, str]] = {}
    for module in modules:
        try:
            source_root, package = _python_package_context(repo_root, module)
        except ValueError:
            continue
        python_contexts[module] = (source_root, package)
        python_lookup.setdefault((source_root, package), set()).add(module)
    module_paths = frozenset(python_contexts)

    dependencies: set[tuple[str, str]] = set()
    complete_sources: set[str] = set()
    for module_a, (source_root, current_package) in python_contexts.items():
        source_paths = _resolve_module_sources(repo_root, module_a)
        if not source_paths:
            continue
        complete = True
        for source_path in source_paths:
            try:
                content = source_path.read_text(
                    encoding="utf-8",
                    errors="replace",
                )
            except (OSError, PermissionError):
                complete = False
                continue
            if source_path.suffix == ".py":
                targets = _python_import_targets(
                    content,
                    current_package=current_package,
                )
                if targets is None:
                    complete = False
                    continue
                for target in targets:
                    module_b = _match_python_module(
                        target,
                        source_root,
                        python_lookup,
                    )
                    if module_b is not None and module_b != module_a:
                        dependencies.add((module_a, module_b))
            else:
                for match in _TS_IMPORT_SPECIFIER_RE.finditer(content):
                    module_b = _match_typescript_module(
                        repo_root,
                        source_path,
                        match.group("specifier"),
                        module_paths,
                    )
                    if module_b is not None and module_b != module_a:
                        dependencies.add((module_a, module_b))
        if complete:
            complete_sources.add(module_a)
    return ModuleDependencyEvidence(
        positive_pairs=frozenset(dependencies),
        complete_sources=frozenset(complete_sources),
    )


def select_balanced_dependency_pairs(
    modules: list[str],
    positive_pairs: frozenset[tuple[str, str]],
    count: int,
    *,
    negative_sources: frozenset[str] | None = None,
) -> list[tuple[tuple[str, str], str]]:
    """Select equal known-positive and known-negative dependency pairs."""
    eligible_negative_sources = (
        frozenset(modules) if negative_sources is None else negative_sources
    )
    total_pairs = len(eligible_negative_sources) * (len(modules) - 1)
    positive_pairs_with_complete_source = sum(
        module_a in eligible_negative_sources for module_a, _ in positive_pairs
    )
    per_polarity = min(
        len(positive_pairs),
        total_pairs - positive_pairs_with_complete_source,
        count // 2,
    )
    if per_polarity == 0:
        return []

    selected_positive = random.sample(sorted(positive_pairs), per_polarity)
    selected_negative: set[tuple[str, str]] = set()
    for _ in range(max(100, per_polarity * 50)):
        if len(selected_negative) == per_polarity:
            break
        module_a, module_b = random.sample(modules, 2)
        pair = (module_a, module_b)
        if module_a in eligible_negative_sources and pair not in positive_pairs:
            selected_negative.add(pair)

    balance = min(len(selected_positive), len(selected_negative))
    selected = [
        *((pair, "yes") for pair in selected_positive[:balance]),
        *((pair, "no") for pair in sorted(selected_negative)[:balance]),
    ]
    random.shuffle(selected)
    return selected


__all__ = [
    "ModuleDependencyEvidence",
    "check_module_dependency",
    "discover_module_dependencies",
    "scan_module_dependencies",
    "select_balanced_dependency_pairs",
]
