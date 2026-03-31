"""Preamble data model — composable instruction template blocks."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PreambleBlock:
    """A named, composable instruction template block.

    Template strings may contain ``{{variable}}`` placeholders that are
    resolved at composition time via :meth:`render`.
    """

    name: str
    template: str
    description: str = ""

    def render(self, context: dict[str, str]) -> str:
        """Render template by substituting ``{{key}}`` placeholders.

        Unknown variables are left intact (no crash, no silent removal).
        """
        result = self.template
        for key, value in context.items():
            result = result.replace("{{" + key + "}}", value)
        return result
