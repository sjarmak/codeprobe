"""Content policy — strip secrets before events enter the trace store.

Applied to every ``tool_input`` and ``tool_output`` string before the
recorder computes bytes or issues an INSERT. Three mechanical passes:

1. **Env-value scan** — any substring equal to a live ``os.environ``
   value (length >= 8) is replaced with ``[REDACTED-ENV]``. Matching is
   exact string membership — no regex, no keyword heuristic — because
   we know the exact values at construction time.
2. **Auth regex** — Authorization / X-Api-Key / AWS session token / GCP
   ``ya29.*`` bearer patterns replaced with ``[REDACTED-AUTH]``.
3. **Deny-glob** — if any user-supplied ``fnmatch`` pattern matches the
   full string, replace the entire value with ``[REDACTED-GLOB]``.

Order is fixed (env → auth → glob) so the most specific redaction wins
and later passes can't re-expose earlier redactions.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field

# Minimum env-value length. Shorter values (e.g. ``TERM=xterm``) produce
# too many false positives — the value "xterm" appears legitimately in
# tool output and redacting it corrupts traces. 8 is the cutoff used by
# most secret scanners (GitHub, AWS, etc.).
_MIN_ENV_VALUE_LEN = 8

# Auth patterns — compiled once. Intentionally greedy on the tail so
# multi-token headers ("Bearer abc.def.ghi") collapse to a single redaction.
_AUTH_HEADER_RE = re.compile(
    r"(?:authorization|x-api-key|x-amz-security-token|aws_session_token)"
    r"\s*[:=]\s*\S+",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE)
# Google OAuth access tokens always start ``ya29.`` and contain 20+
# URL-safe base64 chars.
_GCP_BEARER_RE = re.compile(r"ya29\.[A-Za-z0-9_\-]{20,}")

_AUTH_PATTERNS: tuple[re.Pattern[str], ...] = (
    _AUTH_HEADER_RE,
    _BEARER_RE,
    _GCP_BEARER_RE,
)

REDACTED_ENV = "[REDACTED-ENV]"
REDACTED_AUTH = "[REDACTED-AUTH]"
REDACTED_GLOB = "[REDACTED-GLOB]"


def _snapshot_env_values() -> frozenset[str]:
    """Capture env values eligible for redaction (length >= 8)."""
    return frozenset(
        v for v in os.environ.values() if isinstance(v, str) and len(v) >= _MIN_ENV_VALUE_LEN
    )


@dataclass(frozen=True)
class ContentPolicy:
    """Immutable redaction policy applied to every trace field.

    ``env_values`` is captured at construction time — callers who
    rotate secrets must rebuild the policy. ``deny_globs`` applies to
    tool *output* only: any string fully matching one of the
    ``fnmatch``-style patterns is replaced wholesale with
    ``[REDACTED-GLOB]``.
    """

    env_values: frozenset[str] = field(default_factory=_snapshot_env_values)
    deny_globs: tuple[str, ...] = ()

    def apply(self, text: str | None, *, is_output: bool = False) -> str | None:
        """Return *text* with env values, auth patterns, and deny-globs stripped.

        ``None`` passes through unchanged so the caller doesn't have to
        special-case nullable fields. ``is_output`` enables the
        deny-glob pass; input strings skip it (globs are specifically
        for tool output per the work-unit spec).
        """
        if text is None:
            return None
        if not text:
            return text

        # Pass 1 — env values. Straight string.replace per distinct value.
        # Set-membership is implicit: only values in the snapshot get
        # replaced, and each is replaced in one pass.
        redacted = text
        for value in self.env_values:
            if value and value in redacted:
                redacted = redacted.replace(value, REDACTED_ENV)

        # Pass 2 — auth regexes. Each pattern runs independently.
        for pattern in _AUTH_PATTERNS:
            redacted = pattern.sub(REDACTED_AUTH, redacted)

        # Pass 3 — deny-globs (output only). If any glob matches the
        # redacted string in full, the whole field is replaced.
        if is_output and self.deny_globs:
            for glob in self.deny_globs:
                if fnmatch.fnmatch(redacted, glob):
                    return REDACTED_GLOB

        return redacted
