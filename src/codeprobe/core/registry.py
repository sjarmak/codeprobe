"""Agent adapter registry — resolve adapters by name."""

from __future__ import annotations

import importlib
import importlib.metadata
from typing import Any


def _import_class(dotted: str) -> type:
    """Import a class from a 'module.path:ClassName' string."""
    module_path, class_name = dotted.rsplit(":", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)  # type: ignore[no-any-return]


def _resolve(
    name: str,
    builtins: dict[str, str],
    ep_group: str,
    kind: str,
) -> Any:
    """Generic resolve: check builtins, then entry_points, raise KeyError."""
    if name in builtins:
        try:
            cls = _import_class(builtins[name])
        except ImportError as exc:
            raise KeyError(
                f"{kind} {name!r} could not be loaded — a required dependency "
                f"is missing: {exc}. Check that the tool is installed."
            ) from exc
        return cls()

    eps = importlib.metadata.entry_points(group=ep_group)
    for ep in eps:
        if ep.name == name:
            try:
                cls = ep.load()
            except ImportError as exc:
                raise KeyError(
                    f"{kind} {name!r} could not be loaded — a required dependency "
                    f"is missing: {exc}. Check that the tool is installed."
                ) from exc
            return cls()

    all_names = _available(builtins, ep_group)
    raise KeyError(f"Unknown {kind}: {name!r}. Available: {', '.join(all_names)}")


def _available(builtins: dict[str, str], ep_group: str) -> list[str]:
    """Generic available: merge builtins with entry_points."""
    names = set(builtins.keys())
    eps = importlib.metadata.entry_points(group=ep_group)
    names.update(ep.name for ep in eps)
    return sorted(names)


# -- Agent adapter registry ---------------------------------------------------

_BUILTINS: dict[str, str] = {
    "claude": "codeprobe.adapters.claude:ClaudeAdapter",
    "codex": "codeprobe.adapters.codex:CodexAdapter",
    "copilot": "codeprobe.adapters.copilot:CopilotAdapter",
}


def resolve(name: str) -> Any:
    """Resolve an agent adapter by name. Raises KeyError if not found."""
    return _resolve(name, _BUILTINS, "codeprobe.agents", "agent adapter")


def available() -> list[str]:
    """Return sorted list of all registered agent names."""
    return _available(_BUILTINS, "codeprobe.agents")


# -- Session collector registry -----------------------------------------------

_SESSION_BUILTINS: dict[str, str] = {
    "claude": "codeprobe.adapters.session:ClaudeSessionCollector",
    "codex": "codeprobe.adapters.session:CodexSessionCollector",
    "copilot": "codeprobe.adapters.session:CopilotSessionCollector",
}


def resolve_session(name: str) -> Any:
    """Resolve a session collector by name. Raises KeyError if not found."""
    return _resolve(name, _SESSION_BUILTINS, "codeprobe.sessions", "session collector")


def available_sessions() -> list[str]:
    """Return sorted list of all registered session collector names."""
    return _available(_SESSION_BUILTINS, "codeprobe.sessions")


# -- Scorer registry ----------------------------------------------------------

_SCORER_BUILTINS: dict[str, str] = {
    "artifact": "codeprobe.core.scoring:ArtifactScorer",
    "binary": "codeprobe.core.scoring:BinaryScorer",
    "continuous": "codeprobe.core.scoring:ContinuousScorer",
    "checkpoint": "codeprobe.core.scoring:CheckpointScorer",
    "test_ratio": "codeprobe.core.scoring:ContinuousScorer",
}


def resolve_scorer(name: str) -> Any:
    """Resolve a scorer by name. Raises KeyError if not found."""
    return _resolve(name, _SCORER_BUILTINS, "codeprobe.scorers", "scorer")


def available_scorers() -> list[str]:
    """Return sorted list of all registered scorer names."""
    return _available(_SCORER_BUILTINS, "codeprobe.scorers")
