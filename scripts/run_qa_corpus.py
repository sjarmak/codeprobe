"""One-shot script: run benchmark_qa_core checks across the canonical corpus.

Walks ``examples/``, ``.codeprobe/tasks/``, and ``e2e-codeprobe-self/tasks/``
under the repo root, runs ``verify_task_qa`` on each, and emits a
markdown-friendly summary on stdout.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

from codeprobe.qa.verify import verify_task_qa


CORPUS_ROOTS: tuple[Path, ...] = (
    Path("examples"),
    Path(".codeprobe/tasks"),
    Path("e2e-codeprobe-self/tasks"),
    Path("tests/fixtures"),
)


def _is_task_dir(p: Path) -> bool:
    return (p / "task.toml").is_file() or (p / "metadata.json").is_file()


def _enumerate_tasks(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    found: list[Path] = []
    for child in root.rglob("*"):
        if not child.is_dir():
            continue
        if _is_task_dir(child):
            found.append(child)
    return sorted(found)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--no-repo-root-hint",
        action="store_true",
        help="Skip the repo_root override for mined tasks (surfaces the "
        "out-of-the-box default-repo_root false-positive class).",
    )
    args = parser.parse_args()

    repo_root = Path.cwd()
    all_tasks: list[Path] = []
    for root in CORPUS_ROOTS:
        all_tasks.extend(_enumerate_tasks(repo_root / root))

    print(f"# benchmark_qa_core run — {len(all_tasks)} tasks\n")

    error_counter: Counter[str] = Counter()
    warning_counter: Counter[str] = Counter()
    info_counter: Counter[str] = Counter()
    failures: list[tuple[Path, list[str]]] = []
    warnings: list[tuple[Path, list[str]]] = []

    for task_dir in all_tasks:
        # Mined codeprobe-self tasks reference src/ files at the repo root.
        # Pass repo_root explicitly for those; otherwise default
        # (task_dir-relative) is fine for synthetic example tasks that
        # carry their oracle files inside the bundle.
        rel = task_dir.relative_to(repo_root)
        rel_root_parts = rel.parts
        is_mined = (
            rel_root_parts[:1] == ("e2e-codeprobe-self",)
            or rel_root_parts[:1] == (".codeprobe",)
        )
        explicit_root = (
            repo_root if (is_mined and not args.no_repo_root_hint) else None
        )
        result = verify_task_qa(task_dir, repo_root=explicit_root)
        for f in result.findings:
            if f.severity == "error":
                error_counter[f.code] += 1
            elif f.severity == "warning":
                warning_counter[f.code] += 1
            else:
                info_counter[f.code] += 1
        errs = [f"{f.code}:{f.message}" for f in result.findings if f.severity == "error"]
        warns = [
            f"{f.code}:{f.message}" for f in result.findings if f.severity == "warning"
        ]
        if errs:
            failures.append((rel, errs))
        if warns:
            warnings.append((rel, warns))

    ok_count = len(all_tasks) - len(failures)
    print(f"## Summary\n")
    print(f"- tasks scanned: **{len(all_tasks)}**")
    print(f"- error-clean: **{ok_count}**")
    print(f"- with errors: **{len(failures)}**")
    print(f"- with warnings: **{len(warnings)}**")
    print()

    print(f"## Finding-code histogram\n")
    print("| code | severity | count |")
    print("|------|----------|-------|")
    for code, n in sorted(error_counter.items()):
        print(f"| {code} | error | {n} |")
    for code, n in sorted(warning_counter.items()):
        print(f"| {code} | warning | {n} |")
    for code, n in sorted(info_counter.items()):
        print(f"| {code} | info | {n} |")
    print()

    if failures:
        print(f"## Tasks with errors\n")
        for rel, errs in failures:
            print(f"### `{rel}`")
            for e in errs:
                print(f"- {e}")
            print()

    if warnings:
        print(f"## Tasks with warnings\n")
        for rel, warns in warnings:
            print(f"### `{rel}`")
            for w in warns:
                print(f"- {w}")
            print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
