"""Offline-mode helpers for network-touching subsystems.

Provides :func:`guard_offline`, a small gate that subsystems invoke
immediately before any HTTP/network I/O. When ``CODEPROBE_OFFLINE`` is set
to a truthy value (see :func:`codeprobe.net.is_offline_mode`), the gate
raises a :class:`codeprobe.cli.errors.DiagnosticError` with code
``OFFLINE_NET_ATTEMPT`` so the caller aborts fast and the CLI renders a
structured error envelope (per the Agent-Friendly CLI PRD §12.Q9).

Design notes
------------

* Socket-level interception is intentionally out of scope (Agent-Friendly
  CLI PRD §NG6) — the gate is opt-in, placed at each known network call
  site, rather than a blanket monkeypatch of ``socket.socket``.
* **NG6 resolution (2026-04-28, bead codeprobe-k67): won't-fix.** The
  follow-up PRD was deferred and re-evaluated. Socket-level enforcement
  is not pursued because:

  1. The call-site gate covers every known network entrypoint —
     :func:`guard_offline` is invoked at all eight HTTP call sites in
     ``src/codeprobe/mining/`` (``sources.py``, ``curator_backends.py``,
     ``vcs/_http.py``, ``adapters/pr.py``, ``extractor.py`` ×3,
     ``sg_ground_truth.py``) and ``OFFLINE_NET_ATTEMPT`` is a terminal
     :class:`~codeprobe.cli.errors.DiagnosticError`.
  2. Adapters are subprocess-based (``gh``, sourcegraph CLI, agent
     binaries), so the agent process owns its own network IO. A
     Python-level ``socket.socket`` monkeypatch would not intercept it.
  3. Python-side socket interception is fragile across IO paths
     (``urllib3``, ``asyncio`` transports, raw sockets, ``ssl``); any
     sound implementation would have to be OS-level (network namespaces
     or iptables), which is outside codeprobe's runtime contract.
  4. No offline-mode leaks have been observed in production since v0.7.0.

  Re-open NG6 only if a concrete leak is observed that the call-site
  gate cannot cover. New network-touching code paths MUST add a
  :func:`guard_offline` call at the boundary; that is the contract this
  module enforces.
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
