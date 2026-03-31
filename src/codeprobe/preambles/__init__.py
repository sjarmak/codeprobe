"""Built-in preamble blocks shipped with codeprobe."""

from __future__ import annotations

from pathlib import Path

from codeprobe.models.preamble import PreambleBlock

_PREAMBLES_DIR = Path(__file__).resolve().parent

_BUILTIN_CACHE: dict[str, PreambleBlock] = {}


def _load_builtin(name: str) -> PreambleBlock | None:
    """Load a built-in preamble by name, returning None if not found."""
    if "/" in name or "\\" in name or ".." in name:
        return None

    if name in _BUILTIN_CACHE:
        return _BUILTIN_CACHE[name]

    path = (_PREAMBLES_DIR / f"{name}.md").resolve()
    if not str(path).startswith(str(_PREAMBLES_DIR) + "/"):
        return None
    if not path.is_file():
        return None

    block = PreambleBlock(
        name=name,
        template=path.read_text(encoding="utf-8").strip(),
        description=f"Built-in {name} preamble",
    )
    _BUILTIN_CACHE[name] = block
    return block


def get_builtin(name: str) -> PreambleBlock:
    """Get a built-in preamble by name.

    Raises ``KeyError`` if the preamble does not exist.
    """
    block = _load_builtin(name)
    if block is None:
        raise KeyError(f"No built-in preamble named {name!r}")
    return block


def list_builtins() -> list[str]:
    """Return names of all available built-in preambles."""
    return sorted(
        p.stem for p in _PREAMBLES_DIR.glob("*.md")
    )
