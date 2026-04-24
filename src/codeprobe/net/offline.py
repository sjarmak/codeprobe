"""Offline-mode helpers for network-touching subsystems.

Provides :func:`guard_offline`, a small gate that subsystems invoke
immediately before any HTTP/network I/O. When ``CODEPROBE_OFFLINE`` is set
to a truthy value (see :func:`codeprobe.net.is_offline_mode`), the gate
raises a :class:`codeprobe.cli.errors.DiagnosticError` with code
``OFFLINE_NET_ATTEMPT`` so the caller aborts fast and the CLI renders a
structured error envelope (per the Agent-Friendly CLI PRD §12.Q9).

Design notes
------------

* Socket-level interception is intentionally out of scope (PRD NG6) — the
  gate is opt-in, placed at each known network call site, rather than a
  blanket monkeypatch of ``socket.socket``.
* ``DiagnosticError`` is imported lazily inside :func:`guard_offline` to
  avoid a circular import: ``codeprobe.net`` must be importable from
  ``codeprobe.cli.errors`` transitively, and importing at module load
  would reverse that direction.
"""

from __future__ import annotations

from codeprobe.net import is_offline_mode

__all__ = ["guard_offline"]


_DIAGNOSE_CMD = "codeprobe check-infra offline --json"


def guard_offline(context: str | None = None) -> None:
    """Raise ``OFFLINE_NET_ATTEMPT`` when ``CODEPROBE_OFFLINE`` is set.

    Call this immediately before any HTTP / subprocess-based network I/O
    in the mining and adapter code paths. When offline mode is not
    active, the call is a no-op.

    Parameters
    ----------
    context:
        A short free-form string describing what the caller was trying to
        reach (e.g., ``"sourcegraph GraphQL"``, ``"github via gh"``).
        Included verbatim in the error message so the user can pinpoint
        which subsystem tripped the gate.

    Raises
    ------
    codeprobe.cli.errors.DiagnosticError
        When ``CODEPROBE_OFFLINE`` is truthy. The error carries
        ``code="OFFLINE_NET_ATTEMPT"``, ``terminal=True``, and a
        ``diagnose_cmd`` pointing at ``codeprobe check-infra offline``.
    """

    if not is_offline_mode():
        return

    # Lazy import: ``codeprobe.cli.errors`` and its transitive deps may
    # import ``codeprobe.net``, so importing at module load would create
    # a cycle during package initialization.
    from codeprobe.cli.errors import DiagnosticError

    message = "Network call attempted with CODEPROBE_OFFLINE=1"
    if context:
        message = f"{message} ({context})"
    raise DiagnosticError(
        code="OFFLINE_NET_ATTEMPT",
        message=message,
        diagnose_cmd=_DIAGNOSE_CMD,
        terminal=True,
    )
