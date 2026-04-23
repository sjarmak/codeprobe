"""VCS adapters for mining merge/pull requests from hosted git platforms."""

from codeprobe.mining.vcs.base import (
    AuthFailure,
    AuthMode,
    MergeRequest,
    VCSAdapter,
    redact,
)
from codeprobe.mining.vcs.gitlab import GitLabAdapter

__all__ = [
    "AuthFailure",
    "AuthMode",
    "GitLabAdapter",
    "MergeRequest",
    "VCSAdapter",
    "redact",
]
