"""Tests for :class:`codeprobe.mining.ast_resolver.AstResolver`.

Covers:

- Python: bare calls, method calls, definitions, import filtering
- Go: method declarations, method calls vs. package-qualified calls
- Scope handling: ``defining_file`` restricts results to the same package
- Protocol conformance with :class:`SymbolResolver`
- Performance bound: 1000-file Go repo scan completes well under 30s
- Integration: gascity ``MkdirAll`` example, package-scoped to fake.go
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from codeprobe.mining.ast_resolver import AstResolver
from codeprobe.mining.multi_repo import FileRef, Symbol, SymbolResolver


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_ast_resolver_satisfies_symbol_resolver_protocol() -> None:
    resolver = AstResolver()
    assert isinstance(resolver, SymbolResolver)


def test_invalid_scope_raises() -> None:
    with pytest.raises(ValueError, match="invalid scope"):
        AstResolver(scope="not-a-scope")


def test_empty_inputs_return_empty_list(tmp_path: Path) -> None:
    r = AstResolver()
    assert r.find_references("", [str(tmp_path)]) == []
    assert r.find_references("Foo", []) == []


def test_non_directory_repo_skipped(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-dir"
    r = AstResolver()
    assert r.find_references("Foo", [str(missing)]) == []


# ---------------------------------------------------------------------------
# Python AST behavior
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_python_finds_definition_and_caller(tmp_path: Path) -> None:
    _write(
        tmp_path / "lib.py",
        "def my_func():\n    return 1\n",
    )
    _write(
        tmp_path / "user.py",
        "from lib import my_func\n\nresult = my_func()\n",
    )

    r = AstResolver()
    refs = r.find_references("my_func", [str(tmp_path)])
    paths = sorted(ref.path for ref in refs)
    assert paths == ["lib.py", "user.py"]


def test_python_method_call_on_local_object(tmp_path: Path) -> None:
    _write(
        tmp_path / "lib.py",
        "class C:\n    def my_method(self):\n        return 1\n",
    )
    _write(
        tmp_path / "user.py",
        "from lib import C\n\nc = C()\nc.my_method()\n",
    )

    r = AstResolver()
    refs = r.find_references("my_method", [str(tmp_path)])
    paths = sorted(ref.path for ref in refs)
    # lib.py defines my_method; user.py calls c.my_method() (c is local).
    assert paths == ["lib.py", "user.py"]


def test_python_skips_imported_module_attribute(tmp_path: Path) -> None:
    """``mod.foo()`` where ``mod`` is an imported module should NOT match
    a search for symbol ``foo`` in OTHER files.

    The defining module file (``mod.py``) still matches because the
    definition itself uses ``def foo``.
    """
    _write(
        tmp_path / "mod.py",
        "def foo():\n    return 1\n",
    )
    _write(
        tmp_path / "consumer.py",
        # Treats ``mod.foo()`` as a qualified call into an imported
        # module — the resolver should NOT count this as a method call.
        # However, ``from mod import foo`` IS counted via ImportFrom.
        # Only ``mod.foo()`` style should be filtered.
        "import mod\nresult = mod.foo()\n",
    )
    _write(
        tmp_path / "unrelated.py",
        "x = 5\n",
    )

    r = AstResolver()
    refs = r.find_references("foo", [str(tmp_path)])
    paths = sorted(ref.path for ref in refs)
    # mod.py contains the definition. consumer.py is filtered because
    # ``mod.foo`` is treated as imported-module access, not method call.
    assert paths == ["mod.py"]


def test_python_resolve_symbol_at(tmp_path: Path) -> None:
    src = "def alpha():\n    pass\n\nclass Beta:\n    pass\n"
    _write(tmp_path / "x.py", src)

    r = AstResolver()
    sym = r.resolve_symbol_at(str(tmp_path), "x.py", 1)
    assert sym == Symbol(name="alpha", repo=tmp_path.name, path="x.py")
    sym2 = r.resolve_symbol_at(str(tmp_path), "x.py", 4)
    assert sym2 == Symbol(name="Beta", repo=tmp_path.name, path="x.py")
    assert r.resolve_symbol_at(str(tmp_path), "x.py", 99) is None


def test_python_unparseable_file_skipped(tmp_path: Path) -> None:
    """Files with SyntaxError must not abort the scan."""
    _write(tmp_path / "broken.py", "def )(:\n  bogus\n")
    _write(tmp_path / "good.py", "def foo():\n    return 1\n")

    r = AstResolver()
    refs = r.find_references("foo", [str(tmp_path)])
    paths = sorted(ref.path for ref in refs)
    assert "good.py" in paths
    # broken.py contributes nothing but does not raise.


# ---------------------------------------------------------------------------
# Go AST behavior — gated on the go toolchain being available
# ---------------------------------------------------------------------------


_HAS_GO = shutil.which("go") is not None
_REQUIRES_GO = pytest.mark.skipif(
    not _HAS_GO, reason="go toolchain not installed; AstResolver.go path skipped"
)


@_REQUIRES_GO
def test_go_finds_method_decl_and_call(tmp_path: Path) -> None:
    _write(
        tmp_path / "fake.go",
        """package fake

type Fake struct{}

func (f *Fake) Hello() string {
    return "hi"
}
""",
    )
    _write(
        tmp_path / "user.go",
        """package fake

func use() string {
    f := &Fake{}
    return f.Hello()
}
""",
    )

    r = AstResolver()
    refs = r.find_references("Hello", [str(tmp_path)])
    paths = sorted(ref.path for ref in refs)
    assert paths == ["fake.go", "user.go"]


@_REQUIRES_GO
def test_go_skips_package_qualified_call(tmp_path: Path) -> None:
    """``os.MkdirAll`` should not be counted as a method call on a
    locally-typed value when ``os`` is an imported package."""
    _write(
        tmp_path / "main.go",
        """package main

import "os"

func main() {
    _ = os.MkdirAll("/tmp/x", 0o755)
}
""",
    )
    r = AstResolver()
    refs = r.find_references("MkdirAll", [str(tmp_path)])
    # No method declarations and no non-package-qualified calls.
    assert refs == []


@_REQUIRES_GO
def test_go_unparseable_file_skipped(tmp_path: Path) -> None:
    _write(tmp_path / "broken.go", "this is not valid Go {\n")
    _write(
        tmp_path / "good.go",
        "package x\n\nfunc Foo() int { return 1 }\n",
    )
    r = AstResolver()
    refs = r.find_references("Foo", [str(tmp_path)])
    paths = sorted(ref.path for ref in refs)
    assert "good.go" in paths


# ---------------------------------------------------------------------------
# Scope handling
# ---------------------------------------------------------------------------


def test_scope_package_restricts_to_defining_dir(tmp_path: Path) -> None:
    _write(tmp_path / "pkg" / "lib.py", "def f():\n    return 1\n")
    _write(tmp_path / "pkg" / "neighbor.py", "from .lib import f\nf()\n")
    _write(
        tmp_path / "other" / "consumer.py",
        "def f():\n    return 2\n",
    )

    r = AstResolver(defining_file="pkg/lib.py")
    refs = r.find_references("f", [str(tmp_path)])
    paths = sorted(ref.path for ref in refs)
    # The other/consumer.py also defines f, but it's outside the
    # defining file's package, so it must be excluded.
    assert paths == ["pkg/lib.py", "pkg/neighbor.py"]


def test_scope_repo_keeps_everything(tmp_path: Path) -> None:
    _write(tmp_path / "a" / "x.py", "def foo():\n    return 1\n")
    _write(tmp_path / "b" / "y.py", "def foo():\n    return 2\n")
    r = AstResolver(defining_file="a/x.py", scope="repo")
    refs = r.find_references("foo", [str(tmp_path)])
    paths = sorted(ref.path for ref in refs)
    assert paths == ["a/x.py", "b/y.py"]


# ---------------------------------------------------------------------------
# Performance bound
# ---------------------------------------------------------------------------


@_REQUIRES_GO
def test_perf_bound_under_30s_for_1000_file_repo(tmp_path: Path) -> None:
    """Generate a 1000-file synthetic Go repo and confirm the scan
    completes well under the 30 s perf bound from the bead.
    """
    for i in range(1000):
        pkg_dir = tmp_path / f"pkg{i // 50}"
        _write(
            pkg_dir / f"f{i}.go",
            f"package pkg{i // 50}\n\nfunc Helper{i}() int {{ return {i} }}\n",
        )

    r = AstResolver()
    start = time.perf_counter()
    refs = r.find_references("Helper42", [str(tmp_path)])
    elapsed = time.perf_counter() - start
    assert any(ref.path.endswith("f42.go") for ref in refs)
    assert elapsed < 30.0, f"AstResolver took {elapsed:.1f}s on 1000-file Go repo"


# ---------------------------------------------------------------------------
# Integration: gascity MkdirAll (skipped when checkout absent)
# ---------------------------------------------------------------------------


_GASCITY = Path("/home/ds/gascity")
_HAS_GASCITY = (_GASCITY / "internal" / "fsys" / "fake.go").is_file()
_REQUIRES_GASCITY = pytest.mark.skipif(
    not (_HAS_GO and _HAS_GASCITY),
    reason="gascity checkout or go toolchain not available",
)


@_REQUIRES_GASCITY
def test_gascity_mkdirall_intra_package_scope() -> None:
    """AstResolver scoped to fake.go finds the same intra-package
    callers SG returns: 4 files in ``internal/fsys/``.

    Cross-package callers require type inference and are explicitly
    out of scope for AstResolver v1; the README documents this gap.
    """
    r = AstResolver(defining_file="internal/fsys/fake.go")
    refs = r.find_references("MkdirAll", [str(_GASCITY)])
    paths = sorted(ref.path for ref in refs)
    assert paths == [
        "internal/fsys/atomic_internal_test.go",
        "internal/fsys/fake.go",
        "internal/fsys/fake_test.go",
        "internal/fsys/fsys.go",
    ]
    # Every ref FileRef is anchored on the gascity repo name.
    assert {ref.repo for ref in refs} == {_GASCITY.name}


@_REQUIRES_GASCITY
def test_gascity_runs_offline() -> None:
    """The AST backend must not call Sourcegraph; we assert this
    indirectly by running with all SG-related env vars cleared.
    """
    import os

    original = {
        k: os.environ.pop(k, None)
        for k in (
            "SRC_ACCESS_TOKEN",
            "SOURCEGRAPH_TOKEN",
            "SOURCEGRAPH_ACCESS_TOKEN",
        )
    }
    try:
        r = AstResolver(defining_file="internal/fsys/fake.go")
        refs = r.find_references("MkdirAll", [str(_GASCITY)])
        assert len(refs) == 4
    finally:
        for k, v in original.items():
            if v is not None:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# Returns FileRef instances with sensible repo names
# ---------------------------------------------------------------------------


def test_find_references_returns_fileref(tmp_path: Path) -> None:
    _write(tmp_path / "x.py", "def s():\n    return 1\n")
    r = AstResolver()
    refs = r.find_references("s", [str(tmp_path)])
    assert refs and isinstance(refs[0], FileRef)
    assert refs[0].repo == tmp_path.name
