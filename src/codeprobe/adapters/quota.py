"""Shared quota / rate-limit exhaustion detection for CLI adapters.

Patterns that indicate a provider quota was exhausted. Detected from raw
CLI text because agent CLIs do not surface these as structured envelopes —
they print a short literal message and exit successfully, which would
otherwise be scored as a 0.0 task failure and silently contaminate the
run mean (codeprobe-9xrl).

Callers are responsible for pre-filtering agent tool I/O out of the text
they pass in (e.g. the Claude adapter's stream-json literal-line filter,
codeprobe-9tk): scanning agent-authored content false-positives whenever
the agent merely edits or reasons about code mentioning rate limits.
"""

from __future__ import annotations

import re

# Robust to wording variants: monthly limits, rate limits, generic
# "quota" terminology. Case-insensitive.
QUOTA_PATTERN = re.compile(
    r"(?i)"
    r"(monthly\s+usage\s+limit"
    r"|rate\s+limit\s+(?:exceeded|reached)"
    r"|quota\s+(?:exceeded|exhausted)"
    r"|usage\s+limit\s+reached"
    # 2026-06 OAuth wording: "You've hit your session limit · resets 1:10pm".
    # Anchored on "hit your" because bare "session limit" appears in agent
    # prose on session-management tasks and must not halt the run.
    r"|hit\s+your\s+session\s+limit)"
)


def detect_quota_error(*streams: str | None) -> str | None:
    """Return the triggering line when any *stream* matches the pattern.

    Streams are scanned in order; the first match wins. Returns the line
    containing the match so the executor can include the exact provider
    wording in the task's error metadata, or ``None`` when no stream
    matches.
    """
    for stream in streams:
        if not stream:
            continue
        match = QUOTA_PATTERN.search(stream)
        if match:
            for line in stream.splitlines():
                if QUOTA_PATTERN.search(line):
                    return line.strip()
            return match.group(0)
    return None
