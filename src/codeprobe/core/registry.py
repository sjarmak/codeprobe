"""Agent adapter registry — resolve adapters by name."""

from __future__ import annotations

import importlib
import importlib.metadata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codeprobe.adapters.protocol import AgentAdapter

_BUILTINS: dict[str, str] = {
    "claude": "codeprobe.adapters.claude:ClaudeAdapter",
    "copilot": "codeprobe.adapters.copilot:CopilotAdapter",
}


def _import_class(dotted: str) -> type:
    """Import a class from a 'module.path:ClassName' string."""
    module_path, class_name = dotted.rsplit(":", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)  # type: ignore[no-any-return]


def resolve(name: str) -> AgentAdapter:
    """Resolve an agent adapter by name.

    Checks built-in adapters first, then entry_points.
    Raises KeyError if not found.
    """
    if name in _BUILTINS:
        cls = _import_class(_BUILTINS[name])
        return cls()  # type: ignore[no-any-return]

    eps = importlib.metadata.entry_points(group="codeprobe.agents")
    for ep in eps:
        if ep.name == name:
            cls = ep.load()
            return cls()  # type: ignore[no-any-return]

    raise KeyError(
        f"Unknown agent adapter: {name!r}. "
        f"Available: {', '.join(available())}"
    )


def available() -> list[str]:
    """Return sorted list of all registered agent names."""
    names = set(_BUILTINS.keys())
    eps = importlib.metadata.entry_points(group="codeprobe.agents")
    names.update(ep.name for ep in eps)
    return sorted(names)
