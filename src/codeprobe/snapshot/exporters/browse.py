"""Self-contained ``browse.html`` exporter.

The generated document is designed for airgapped viewing:

- No ``<link>`` tags.
- No ``src=`` or ``href=`` references to external resources.
- Styles and scripts are inlined via ``<style>`` and ``<script>`` tags.
- The underlying snapshot rows are embedded inline as JSON in a
  ``<script id="snapshot-data" type="application/json">`` block and also
  rendered server-side into a plain HTML ``<table>`` — so the table is
  readable with JavaScript disabled (graceful degradation).
- Minimal JS enables click-to-sort on column headers when JS is available.

All user-controlled text (column names, cell values, manifest fields)
is passed through :func:`html.escape` before being emitted into the HTML
body. The embedded JSON is additionally protected by escaping the closing
``</script>`` sequence so an attacker cannot break out of the data island.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from codeprobe.snapshot.exporters._common import (
    entry_columns,
    load_entries,
    load_manifest,
    project_row,
)

__all__ = ["export_browse"]


def _safe_cell(value: Any) -> str:
    """Return ``value`` escaped for rendering inside a ``<td>``."""
    if value is None:
        return ""
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return html.escape(text, quote=False)


def _safe_json_island(data: dict[str, Any]) -> str:
    """Serialise ``data`` safely for embedding in an HTML ``<script>`` tag.

    The only XSS vector for an inline JSON island is a literal closing
    ``</script>`` sequence inside a string value; we escape it by
    breaking the tag with a Unicode escape that JSON parsers reassemble
    but the HTML parser leaves alone.
    """
    raw = json.dumps(data, sort_keys=True)
    return raw.replace("</", "<\\/")


def _build_table(columns: list[str], rows: list[list[Any]]) -> str:
    header_cells = "".join(f"<th data-col=\"{html.escape(c)}\">{html.escape(c)}</th>" for c in columns)
    body_rows: list[str] = []
    for row in rows:
        cells = "".join(f"<td>{_safe_cell(v)}</td>" for v in row)
        body_rows.append(f"<tr>{cells}</tr>")
    body = "".join(body_rows) or "<tr><td colspan=\"{n}\"><em>No entries in snapshot.</em></td></tr>".format(n=max(len(columns), 1))
    return (
        "<table id=\"snapshot-table\">"
        f"<thead><tr>{header_cells}</tr></thead>"
        f"<tbody>{body}</tbody>"
        "</table>"
    )


_INLINE_CSS = """
body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
       margin: 2rem; max-width: 1200px; color: #1a1a1a; }
h1 { font-size: 1.4rem; }
.meta { color: #555; font-size: 0.9rem; margin-bottom: 1rem; }
table { border-collapse: collapse; width: 100%; font-size: 0.9rem; }
th, td { border: 1px solid #ddd; padding: 4px 8px; text-align: left;
         vertical-align: top; }
th { background: #f5f5f5; cursor: pointer; user-select: none; }
th[data-sort="asc"]::after { content: " \\25B2"; }
th[data-sort="desc"]::after { content: " \\25BC"; }
tr:nth-child(even) td { background: #fafafa; }
.no-js-note { font-size: 0.8rem; color: #666; margin-top: 1rem; }
""".strip()


_INLINE_JS = """
(function () {
  var table = document.getElementById('snapshot-table');
  if (!table) return;
  var headers = table.querySelectorAll('thead th');
  headers.forEach(function (th, idx) {
    th.addEventListener('click', function () {
      var currentDir = th.getAttribute('data-sort');
      var nextDir = currentDir === 'asc' ? 'desc' : 'asc';
      headers.forEach(function (h) { h.removeAttribute('data-sort'); });
      th.setAttribute('data-sort', nextDir);
      var tbody = table.querySelector('tbody');
      var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
      rows.sort(function (a, b) {
        var av = a.children[idx] ? a.children[idx].textContent : '';
        var bv = b.children[idx] ? b.children[idx].textContent : '';
        var na = parseFloat(av);
        var nb = parseFloat(bv);
        var cmp;
        if (!isNaN(na) && !isNaN(nb)) {
          cmp = na - nb;
        } else {
          cmp = av.localeCompare(bv);
        }
        return nextDir === 'asc' ? cmp : -cmp;
      });
      rows.forEach(function (r) { tbody.appendChild(r); });
    });
  });
})();
""".strip()


def _render_html(manifest: dict[str, Any], entries: list[dict[str, Any]]) -> str:
    columns = entry_columns(entries)
    rows = [project_row(e, columns) for e in entries]

    source = html.escape(str(manifest.get("source", "")))
    mode = html.escape(str(manifest.get("mode", "")))
    created_at = html.escape(str(manifest.get("created_at", "")))

    data_payload: dict[str, Any] = {
        "columns": columns,
        "entries": entries,
        "manifest": {
            "mode": manifest.get("mode"),
            "source": manifest.get("source"),
            "created_at": manifest.get("created_at"),
            "schema_version": manifest.get("schema_version"),
        },
    }
    embedded_json = _safe_json_island(data_payload)

    table_html = _build_table(columns, rows)

    return (
        "<!doctype html>\n"
        "<html lang=\"en\">\n"
        "<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<title>codeprobe snapshot</title>\n"
        f"<style>{_INLINE_CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        "<h1>codeprobe snapshot</h1>\n"
        "<div class=\"meta\">"
        f"<div><strong>source:</strong> {source}</div>"
        f"<div><strong>mode:</strong> {mode}</div>"
        f"<div><strong>created_at:</strong> {created_at}</div>"
        "</div>\n"
        f"{table_html}\n"
        "<noscript><p class=\"no-js-note\">JavaScript is disabled — the "
        "table above is still fully readable; click-to-sort is the only "
        "feature that requires JS.</p></noscript>\n"
        "<p class=\"no-js-note\">This page is fully self-contained and "
        "loads no external resources.</p>\n"
        "<script id=\"snapshot-data\" type=\"application/json\">"
        f"{embedded_json}"
        "</script>\n"
        f"<script>{_INLINE_JS}</script>\n"
        "</body>\n"
        "</html>\n"
    )


def export_browse(snapshot_dir: Path, out_path: Path) -> Path:
    """Render a self-contained ``browse.html`` for ``snapshot_dir``.

    Returns ``out_path`` after writing. The document has no external
    references — the generated file works fully offline.
    """
    snapshot_dir = Path(snapshot_dir)
    out_path = Path(out_path)

    manifest = load_manifest(snapshot_dir)
    entries = load_entries(snapshot_dir)

    rendered = _render_html(manifest, entries)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(rendered, encoding="utf-8")
    return out_path
