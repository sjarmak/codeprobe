"""Observability exporters for codeprobe snapshots.

Each exporter transforms a completed CSB-layout snapshot directory (see
``codeprobe.snapshot.create``) into an artifact tailored for a specific
downstream consumer:

- :func:`export_datadog` — Datadog events/metrics intake JSON.
- :func:`export_sigma` — CSV + dbt-friendly schema JSON for Sigma/Looker.
- :func:`export_sheets` — paste-ready TSV block for Google Sheets.
- :func:`export_browse` — self-contained ``browse.html`` (airgap-safe).

All exporters are pure IO + mechanical transforms: no LLM calls, no network
access, no CDN references in generated artifacts.
"""

from __future__ import annotations

from codeprobe.snapshot.exporters.browse import export_browse
from codeprobe.snapshot.exporters.datadog import export_datadog
from codeprobe.snapshot.exporters.sheets import export_sheets
from codeprobe.snapshot.exporters.sigma import export_sigma

__all__ = [
    "export_browse",
    "export_datadog",
    "export_sheets",
    "export_sigma",
]
