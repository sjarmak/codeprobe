"""Platform dispatch for the no-replace rename used by mining publication."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from codeprobe.mining import _atomic_rename


class _FakeSyscall:
    """A stand-in for a ``ctypes`` libc function pointer."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.argtypes: list[object] = []
        self.restype: object = None
        self.result = 0

    def __call__(self, *args: object) -> int:
        self.calls.append(args)
        return self.result


class _FakeLibc:
    """A libc exposing exactly the symbols a platform is expected to have."""

    def __init__(self, *symbols: str) -> None:
        self.symbols = {name: _FakeSyscall() for name in symbols}

    def __getattr__(self, name: str) -> _FakeSyscall:
        try:
            return self.__dict__["symbols"][name]
        except KeyError:
            raise AttributeError(name) from None


def _install_fake_libc(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    *symbols: str,
) -> _FakeLibc:
    libc = _FakeLibc(*symbols)
    monkeypatch.setattr(_atomic_rename.sys, "platform", platform)
    monkeypatch.setattr(
        _atomic_rename.ctypes,
        "CDLL",
        lambda *args, **kwargs: libc,
    )
    return libc


def _staged(parent: Path, name: str) -> Path:
    staged = parent / name
    staged.mkdir()
    (staged / "artifact.txt").write_text("payload\n")
    return staged


def test_publishes_a_staged_directory_when_the_destination_is_absent(
    tmp_path: Path,
) -> None:
    _staged(tmp_path, "stage")
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        _atomic_rename.rename_component_no_replace(parent_fd, "stage", "final")
    finally:
        os.close(parent_fd)

    assert (tmp_path / "final" / "artifact.txt").read_text() == "payload\n"
    assert not (tmp_path / "stage").exists()


def test_refuses_an_existing_destination_and_leaves_both_intact(
    tmp_path: Path,
) -> None:
    _staged(tmp_path, "stage")
    occupied = tmp_path / "final"
    occupied.mkdir()
    (occupied / "concurrent.txt").write_text("do not clobber\n")

    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError) as excinfo:
            _atomic_rename.rename_component_no_replace(
                parent_fd,
                "stage",
                "final",
            )
    finally:
        os.close(parent_fd)

    assert excinfo.value.errno == errno.EEXIST
    assert (tmp_path / "stage" / "artifact.txt").read_text() == "payload\n"
    assert (occupied / "concurrent.txt").read_text() == "do not clobber\n"


@pytest.mark.parametrize(
    ("platform", "symbol", "flags"),
    [
        ("linux", "renameat2", 0x1),
        ("darwin", "renameatx_np", 0x4),
    ],
)
def test_dispatches_to_the_platform_syscall_with_its_no_replace_flag(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    symbol: str,
    flags: int,
) -> None:
    libc = _install_fake_libc(monkeypatch, platform, symbol)

    _atomic_rename.rename_component_no_replace(7, "stage", "final")

    assert libc.symbols[symbol].calls == [(7, b"stage", 7, b"final", flags)]


def test_a_missing_platform_symbol_names_the_symbol_it_looked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_libc(monkeypatch, "darwin")

    with pytest.raises(OSError) as excinfo:
        _atomic_rename.rename_component_no_replace(7, "stage", "final")

    assert excinfo.value.errno == errno.ENOSYS
    assert "renameatx_np" in str(excinfo.value)


def test_an_unsupported_platform_fails_before_loading_libc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_atomic_rename.sys, "platform", "win32")

    def never_loaded(*args: object, **kwargs: object) -> object:
        pytest.fail("libc must not be loaded on an unsupported platform")

    monkeypatch.setattr(_atomic_rename.ctypes, "CDLL", never_loaded)

    with pytest.raises(OSError) as excinfo:
        _atomic_rename.rename_component_no_replace(7, "stage", "final")

    assert excinfo.value.errno == errno.ENOSYS
    assert "win32" in str(excinfo.value)


def test_a_failing_syscall_reports_errno_and_the_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    libc = _install_fake_libc(monkeypatch, "darwin", "renameatx_np")
    libc.symbols["renameatx_np"].result = -1
    monkeypatch.setattr(
        _atomic_rename.ctypes,
        "get_errno",
        lambda: errno.EXDEV,
    )

    with pytest.raises(OSError) as excinfo:
        _atomic_rename.rename_component_no_replace(7, "stage", "final")

    assert excinfo.value.errno == errno.EXDEV
    assert excinfo.value.filename == "final"
