"""Tests for :class:`codeprobe.mining.ast_resolver.AstResolver`.

Covers:

- Python: bare calls, method calls, definitions, import filtering
- Go: method declarations, local calls, and target package-qualified calls
- Scope handling: ``defining_file`` enables package-aware resolution
- Protocol conformance with :class:`SymbolResolver`
- Performance bound: 1000-file Go repo scan completes well under 30s
- Integration: gascity ``MkdirAll`` example, package-scoped to fake.go
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from codeprobe.mining.ast_resolver import (
    AstResolver,
    _safe_go_env,
    go_toolchain_status,
)
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


def test_unresolved_defining_file_rejects_all_language_evidence(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    _write(primary / "missing" / "other.py", "def Missing():\n    return 1\n")
    _write(secondary / "missing" / "other.py", "def Missing():\n    return 2\n")

    refs = AstResolver(defining_file="missing/def.go").find_references(
        "Missing", [str(primary), str(secondary)]
    )

    assert refs == []


def test_go_versions_before_1_24_are_rejected(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/go")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, "go1.23.9\n", ""
        ),
    )

    with caplog.at_level("WARNING"):
        available = AstResolver()._check_go_binary()

    assert available is False
    assert "Go 1.24 or newer" in caplog.text


def test_go_toolchain_status_does_not_echo_untrusted_version_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/secret/bin/go")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, "malformed\x1b[31m SECRET_VALUE\n", "/secret/error"
        ),
    )

    supported, detail = go_toolchain_status()

    assert supported is False
    assert detail == "unable to determine Go version; Go 1.24 or newer is required"
    assert "SECRET_VALUE" not in detail
    assert "/secret" not in detail


@pytest.mark.parametrize(
    "output",
    [
        "garbage go1.99.0 SECRET_ENV=token",
        "go1.24malformed",
        "go1.25.8\x1b[31m",
    ],
)
def test_go_toolchain_status_rejects_malformed_complete_output(
    monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/go")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, output, ""
        ),
    )

    supported, _ = go_toolchain_status()

    assert supported is False


def test_go_toolchain_status_rejects_oversized_numeric_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/go")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, f"go{'9' * 5000}.24", ""
        ),
    )

    supported, detail = go_toolchain_status()

    assert supported is False
    assert detail == "unrecognized Go version; Go 1.24 or newer is required"


def test_go_toolchain_probe_does_not_inherit_unrelated_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_env: dict[str, str] = {}

    def _capture(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured_env.update(kwargs["env"])  # type: ignore[arg-type]
        return subprocess.CompletedProcess(args[0], 0, "go1.25.8", "")

    monkeypatch.setenv("CODEPROBE_TEST_SENTINEL_SECRET", "do-not-forward")
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/go")
    monkeypatch.setattr(subprocess, "run", _capture)

    supported, _ = go_toolchain_status()

    assert supported is True
    assert captured_env.get("CODEPROBE_TEST_SENTINEL_SECRET") is None


def test_go_scanner_env_does_not_inherit_unrelated_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEPROBE_TEST_SENTINEL_SECRET", "do-not-forward")

    env = _safe_go_env()

    assert env.get("CODEPROBE_TEST_SENTINEL_SECRET") is None


def test_missing_custom_go_binary_path_is_not_disclosed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)

    supported, detail = go_toolchain_status("/private/operator/token/go")

    assert supported is False
    assert "/private" not in detail
    assert detail == "Go executable not found; Go 1.24 or newer is required"


@pytest.mark.parametrize(
    ("output", "displayed"),
    [
        ("go1.24", "go1.24"),
        ("go1.24.0", "go1.24.0"),
        ("go1.24rc1", "go1.24rc1"),
        ("go1.25.8", "go1.25.8"),
        ("go1.24.0-privatefork", "go1.24.0"),
        ("devel go1.26-abcdef", "go1.26 development"),
        (
            "devel go1.24-ffb3e574 Thu Aug 29 20:16:26 2024 +0000",
            "go1.24 development",
        ),
        (
            "go1.25-devel_ffb3e574 Thu Aug 29 20:16:26 2024 +0000",
            "go1.25 development",
        ),
    ],
)
def test_go_toolchain_status_accepts_complete_supported_versions(
    monkeypatch: pytest.MonkeyPatch, output: str, displayed: str
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/go")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, output, ""
        ),
    )

    supported, detail = go_toolchain_status()

    assert supported is True
    assert detail == f"{displayed} (supported)"
    assert "privatefork" not in detail
    assert "ffb3e574" not in detail


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


def test_python_scan_rejects_symlinked_source_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    _write(outside, "def Escaped():\n    return 1\n")
    (repo / "escaped.py").symlink_to(outside)

    refs = AstResolver().find_references("Escaped", [str(repo)])

    assert refs == []


def test_resolve_symbol_at_rejects_paths_outside_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    _write(outside, "def Escaped():\n    return 1\n")
    (repo / "escaped.py").symlink_to(outside)
    resolver = AstResolver()

    assert resolver.resolve_symbol_at(str(repo), "../outside.py", 1) is None
    assert resolver.resolve_symbol_at(str(repo), str(outside), 1) is None
    assert resolver.resolve_symbol_at(str(repo), "escaped.py", 1) is None


def test_resolve_symbol_at_fails_closed_without_nofollow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path / "safe.py", "def Safe():\n    return 1\n")
    monkeypatch.delattr(os, "O_NOFOLLOW")

    assert AstResolver().resolve_symbol_at(str(tmp_path), "safe.py", 1) is None


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
def test_go_finds_calls_through_imported_defining_package(tmp_path: Path) -> None:
    _write(tmp_path / "go.mod", "module example.com/project\n\ngo 1.22\n")
    _write(
        tmp_path / "lib" / "requirev2" / "require.go",
        """package require

func MustNoError() {}
""",
    )
    _write(
        tmp_path / "cmd" / "default" / "main.go",
        """package main

import "example.com/project/lib/requirev2"

func main() { require.MustNoError() }
""",
    )
    _write(
        tmp_path / "cmd" / "alias" / "main.go",
        """package main

import check "example.com/project/lib/requirev2"

func main() { check.MustNoError() }
""",
    )
    _write(
        tmp_path / "cmd" / "unrelated" / "main.go",
        """package main

import other "example.com/other/require"

func main() { other.MustNoError() }
""",
    )

    resolver = AstResolver(defining_file="lib/requirev2/require.go")
    refs = resolver.find_references("MustNoError", [str(tmp_path)])

    assert sorted(ref.path for ref in refs) == [
        "cmd/alias/main.go",
        "cmd/default/main.go",
        "lib/requirev2/require.go",
    ]


@_REQUIRES_GO
def test_go_ignores_shadowed_import_qualifier(tmp_path: Path) -> None:
    _write(tmp_path / "go.mod", "module example.com/project\n\ngo 1.22\n")
    _write(
        tmp_path / "lib" / "require" / "require.go",
        """package require

func MustNoError() {}
func Keep() int { return 1 }
""",
    )
    _write(
        tmp_path / "consumer" / "consumer.go",
        """package consumer

import "example.com/project/lib/require"

type localCheck struct{}
func (localCheck) MustNoError() {}
var _ = require.Keep
func shadow(require localCheck) { require.MustNoError() }
""",
    )

    refs = AstResolver(defining_file="lib/require/require.go").find_references(
        "MustNoError", [str(tmp_path)]
    )

    assert sorted(ref.path for ref in refs) == ["lib/require/require.go"]


@_REQUIRES_GO
def test_go_uses_primary_target_package_across_repositories(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    _write(primary / "go.mod", "module example.com/primary\n\ngo 1.22\n")
    _write(
        primary / "lib" / "require" / "require.go",
        "package require\n\nfunc MustNoError() {}\n",
    )
    _write(secondary / "go.mod", "module example.com/secondary\n\ngo 1.22\n")
    _write(
        secondary / "consumer" / "consumer.go",
        """package consumer

import "example.com/primary/lib/require"

func call() { require.MustNoError() }
""",
    )

    refs = AstResolver(defining_file="lib/require/require.go").find_references(
        "MustNoError", [str(primary), str(secondary)]
    )

    assert sorted((ref.repo, ref.path) for ref in refs) == [
        ("primary", "lib/require/require.go"),
        ("secondary", "consumer/consumer.go"),
    ]


@_REQUIRES_GO
def test_go_unresolved_primary_target_does_not_widen_secondary_scan(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    _write(primary / "go.mod", "module example.com/primary\n\ngo 1.22\n")
    _write(
        secondary / "unrelated.go",
        "package secondary\n\nfunc Missing() {}\n",
    )

    refs = AstResolver(defining_file="missing/missing.go").find_references(
        "Missing", [str(primary), str(secondary)]
    )

    assert refs == []


@_REQUIRES_GO
def test_go_unresolved_target_does_not_widen_primary_scan(tmp_path: Path) -> None:
    _write(
        tmp_path / "unrelated.go",
        "package unrelated\n\nfunc Missing() {}\n",
    )

    refs = AstResolver(defining_file="missing/missing.go").find_references(
        "Missing", [str(tmp_path)]
    )

    assert refs == []


@_REQUIRES_GO
def test_go_resolved_target_without_module_does_not_widen_secondary_scan(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    _write(
        primary / "target" / "target.go",
        "package target\n\nfunc Local() {}\n",
    )
    _write(
        secondary / "unrelated.go",
        "package secondary\n\nfunc Local() {}\n",
    )

    refs = AstResolver(defining_file="target/target.go").find_references(
        "Local", [str(primary), str(secondary)]
    )

    assert sorted((ref.repo, ref.path) for ref in refs) == [
        ("primary", "target/target.go")
    ]


@_REQUIRES_GO
def test_go_scan_rejects_symlinked_source_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.go"
    _write(outside, "package escaped\n\nfunc Escaped() {}\n")
    (repo / "escaped.go").symlink_to(outside)

    refs = AstResolver().find_references("Escaped", [str(repo)])

    assert refs == []


@_REQUIRES_GO
def test_go_scan_rejects_symlinked_defining_package(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    _write(outside / "go.mod", "module malicious.example/target\n")
    _write(outside / "target.go", "package target\n\nfunc Escaped() {}\n")
    (repo / "target").symlink_to(outside, target_is_directory=True)
    _write(
        repo / "caller.go",
        """package caller

import target "malicious.example/target"

func call() { target.Escaped() }
""",
    )

    refs = AstResolver(defining_file="target/target.go").find_references(
        "Escaped", [str(repo)]
    )

    assert refs == []


@_REQUIRES_GO
def test_go_scan_rejects_symlinked_go_mod(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / "target" / "target.go", "package target\n\nfunc Local() {}\n")
    outside_mod = tmp_path / "outside.mod"
    _write(outside_mod, "module malicious.example/project\n")
    (repo / "go.mod").symlink_to(outside_mod)
    _write(
        repo / "caller" / "caller.go",
        """package caller

import target "malicious.example/project/target"

func call() { target.Local() }
""",
    )

    refs = AstResolver(defining_file="target/target.go").find_references(
        "Local", [str(repo)]
    )

    assert sorted(ref.path for ref in refs) == ["target/target.go"]


@_REQUIRES_GO
def test_go_scan_skips_oversized_source_file(tmp_path: Path) -> None:
    source = b"package huge\n\nfunc Huge() {}\n" + b" " * (9 * 1024 * 1024)
    (tmp_path / "huge.go").write_bytes(source)

    refs = AstResolver().find_references("Huge", [str(tmp_path)])

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
# The AST backend resolves without Sourcegraph
# ---------------------------------------------------------------------------


@_REQUIRES_GO
def test_ast_backend_resolves_with_sourcegraph_env_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AstResolver is a local backend and must never need Sourcegraph.

    Asserted indirectly: with every SG credential removed from the
    environment, resolution still returns the intra-package callers.

    This replaces two tests that ran against a hardcoded ``/home/ds/gascity``
    checkout (codeprobe-ghd4, codeprobe-9yk6). Pinning assertions to an
    external repo meant they broke whenever that repo gained a caller — as
    it had, expecting 4 files and finding 6 — while skipping in CI, where no
    such checkout exists, so nobody saw it. The intra-package scoping they
    covered is already asserted synthetically by
    ``test_go_finds_method_decl_and_call`` and
    ``test_go_finds_calls_through_imported_defining_package``; the offline
    property was theirs alone and is kept here.
    """
    for key in ("SRC_ACCESS_TOKEN", "SOURCEGRAPH_TOKEN", "SOURCEGRAPH_ACCESS_TOKEN"):
        monkeypatch.delenv(key, raising=False)

    _write(
        tmp_path / "fake.go",
        """package fsys

type Fake struct{}

func (f *Fake) MkdirAll(path string) error {
    return nil
}
""",
    )
    _write(
        tmp_path / "fsys.go",
        """package fsys

func setup(f *Fake) error {
    return f.MkdirAll("/tmp/x")
}
""",
    )

    refs = AstResolver(defining_file="fake.go").find_references(
        "MkdirAll", [str(tmp_path)]
    )

    assert sorted(ref.path for ref in refs) == ["fake.go", "fsys.go"]
    assert {ref.repo for ref in refs} == {tmp_path.name}


# ---------------------------------------------------------------------------
# Returns FileRef instances with sensible repo names
# ---------------------------------------------------------------------------


def test_find_references_returns_fileref(tmp_path: Path) -> None:
    _write(tmp_path / "x.py", "def s():\n    return 1\n")
    r = AstResolver()
    refs = r.find_references("s", [str(tmp_path)])
    assert refs and isinstance(refs[0], FileRef)
    assert refs[0].repo == tmp_path.name
