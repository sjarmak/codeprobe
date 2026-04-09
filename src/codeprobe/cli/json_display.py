"""JSON Line event consumer for CI pipelines.

Serializes each run lifecycle event as a single JSON line to a file handle
(defaults to stderr).  Designed for machine consumption in CI/CD systems
where structured output is easier to parse than human-readable text.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict
from typing import IO

logger = logging.getLogger(__name__)

from codeprobe.core.events import RunEvent


class JsonLineListener:
    """RunEventListener that emits one JSON object per line.

    Each line contains the ``dataclasses.asdict()`` representation of the
    event plus a ``type`` key holding the class name (e.g. ``"RunStarted"``).

    Parameters
    ----------
    file:
        Writable text stream.  Defaults to ``sys.stderr``.
    """

    def __init__(self, file: IO[str] | None = None) -> None:
        self._file = file or sys.stderr

    def on_event(self, event: RunEvent) -> None:
        """Serialize *event* as a JSON line and write it to the output stream."""
        try:
            payload = asdict(event)  # type: ignore[arg-type]
            payload["type"] = event.__class__.__name__
            line = json.dumps(payload, default=str)
            self._file.write(line + "\n")
            self._file.flush()
        except Exception:
            # Gracefully skip malformed events — never crash the dispatcher.
            logger.debug(
                "Failed to serialize event %s", type(event).__name__, exc_info=True
            )
