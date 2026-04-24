"""VCS adapters for mining merge/pull requests from hosted git platforms."""

from codeprobe.mining.vcs.base import (
    AuthFailureError,
    AuthMode,
    MergeRequest,
    VCSAdapter,
    redact,
)
from codeprobe.mining.vcs.gitlab import GitLabAdapter


def __getattr__(name: str) -> object:
    """Re-export shim for ``AuthFailure`` → ``AuthFailureError`` (N818)."""
    if name == "AuthFailure":
        from codeprobe.mining.vcs.base import __getattr__ as _base_getattr

        return _base_getattr(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AuthFailureError",
    "AuthMode",
    "GitLabAdapter",
    "MergeRequest",
    "VCSAdapter",
    "redact",
]
