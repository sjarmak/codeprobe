"""Deterministic stub :class:`AgentAdapter` for the self-serve E2E harness.

Registered as ``codeprobe.agents`` entry point ``e2e-stub``. Makes the
positive-journey harness runnable in CI with no agent credentials and no
network: ``run()`` never shells out to a real coding-agent CLI or hits an
API — it deterministically edits the workspace in-process and reports
honest telemetry (``cost_source="unavailable"``, matching the fact that no
real billing signal exists for a stub).

This package is test-only infrastructure. It is never a codeprobe runtime
dependency and never ships inside the codeprobe wheel — see
``pyproject.toml`` in this directory for the (empty) dependency list, and
``scripts/e2e/self_serve_acceptance.py`` for where it gets pip-installed
into the harness's throwaway venv, never into a customer's environment.
"""

from __future__ import annotations

import time
from pathlib import Path

from codeprobe.adapters.protocol import (
    AdapterCapabilities,
    AgentConfig,
    AgentOutput,
)

#: Marker text the stub appends to whichever file it edits. Deterministic
#: and greppable, so a harness assertion or a human inspecting a trial
#: worktree can immediately tell the edit came from the stub, not a real
#: agent or a leftover from a previous trial.
_EDIT_MARKER = "# codeprobe-e2e-stub: deterministic edit applied\n"


def _pick_edit_target(workspace: Path) -> Path | None:
    """Choose one source file to edit: the first non-test ``*.py`` file.

    Sorted by relative path for determinism across filesystems/platforms.
    Returns ``None`` when the workspace has no matching file (the stub
    then makes no edit and says so honestly in ``stdout``, rather than
    fabricating one).
    """
    candidates = sorted(
        p
        for p in workspace.rglob("*.py")
        if "tests" not in p.relative_to(workspace).parts
        and not p.name.startswith("test_")
    )
    return candidates[0] if candidates else None


class StubAdapter:
    """No-arg-constructible stub adapter satisfying the AgentAdapter Protocol."""

    @property
    def name(self) -> str:
        return "e2e-stub"

    @property
    def capabilities(self) -> AdapterCapabilities:
        # Grep-verified against run() below: only config.cwd (workspace
        # location) and config.mcp_config (echoed into stdout so a
        # per-arm-config-differs assertion has something real to check)
        # are consumed. Declaring mcp_config=True is what makes a two-arm
        # experiment that differs only on --mcp-config runnable at all —
        # run preflight hard-refuses arms whose knobs the adapter doesn't
        # declare (codeprobe-f7rl.26).
        return AdapterCapabilities(mcp_config=True, workspace_cwd=True)

    def preflight(self, config: AgentConfig) -> list[str]:
        if not config.cwd:
            return ["e2e-stub requires config.cwd to be set (no workspace to edit)"]
        return []

    def isolate_session(self, slot_id: int) -> dict[str, str]:
        # No real session/config-dir state to isolate — the stub is
        # in-process and stateless between trials.
        return {}

    def run(
        self,
        prompt: str,
        config: AgentConfig,
        session_env: dict[str, str] | None = None,
    ) -> AgentOutput:
        start = time.monotonic()
        workspace = Path(config.cwd) if config.cwd else None

        if workspace is None or not workspace.is_dir():
            return AgentOutput(
                stdout="",
                stderr=f"e2e-stub: no valid workspace at cwd={config.cwd!r}",
                exit_code=1,
                duration_seconds=time.monotonic() - start,
                cost_usd=0.0,
                cost_model="unknown",
                cost_source="unavailable",
                error="no workspace to edit",
                error_terminal=True,
            )

        target = _pick_edit_target(workspace)
        if target is None:
            stdout = "e2e-stub: no editable *.py file found; no edit applied\n"
        else:
            with target.open("a", encoding="utf-8") as fh:
                fh.write(_EDIT_MARKER)
            rel = target.relative_to(workspace)
            stdout = f"e2e-stub: appended marker comment to {rel}\n"

        stdout += f"e2e-stub: mcp_config={'set' if config.mcp_config else 'unset'}\n"

        return AgentOutput(
            stdout=stdout,
            stderr=None,
            exit_code=0,
            duration_seconds=time.monotonic() - start,
            # A concrete (not None) cost_usd, even though no real cost was
            # incurred, is deliberate: analysis/report.py's cost-provenance
            # rendering only annotates a summary figure when
            # total_cost_usd is not None (a None total renders a bare "—"
            # with no provenance text at all) — the E2E harness's honesty
            # assertion needs that provenance string present, and
            # cost_source="unavailable" is what makes it read "unknown
            # source" rather than a fabricated api_reported/estimated tag.
            cost_usd=0.0,
            cost_model="unknown",
            cost_source="unavailable",
            input_tokens=0,
            output_tokens=0,
        )


__all__ = ["StubAdapter"]
