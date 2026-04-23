"""AC4 + AC5: ``browse.html`` is airgap-safe and contains snapshot data inline.

We generate the file and then make two classes of assertion:

1. **Airgap**: the document references zero external ``http://`` or
   ``https://`` URLs. ``data:`` and ``file:`` URIs are tolerated because
   they do not require network access.
2. **Inline data**: the underlying aggregate rows are embedded in the
   HTML body (both as a ``<script type="application/json">`` data
   island and as a rendered ``<table>`` so JS-disabled browsers see
   them).
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

from click.testing import CliRunner

from codeprobe.cli import main as cli_main
from tests.snapshot._export_helpers import SYNTHETIC_ENTRIES, build_snapshot


# Match http:// or https:// — case-insensitive — but NOT data:, file:, etc.
_EXTERNAL_URL_RE = re.compile(r"https?://", re.IGNORECASE)


class _TagCollector(HTMLParser):
    """Collect basic structural info to assert airgap invariants."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.external_src_or_href: list[tuple[str, str]] = []
        self.table_present = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self.table_present = True
        if tag == "link":
            # <link> tags are external-resource loads by definition — any
            # appearance is a violation of the airgap contract.
            for name, value in attrs:
                if name == "href" and value:
                    self.external_src_or_href.append((name, value))
        if tag in {"script", "img", "iframe", "audio", "video", "source"}:
            for name, value in attrs:
                if name == "src" and value:
                    # Empty, data:, file: URIs are fine; absolute http(s)
                    # URLs are not.
                    if _EXTERNAL_URL_RE.match(value):
                        self.external_src_or_href.append((name, value))


def _generate_browse_html(tmp_path: Path) -> tuple[Path, str]:
    snap = build_snapshot(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["snapshot", "export", str(snap), "--format", "browse"],
    )
    assert result.exit_code == 0, result.output

    target = snap / "browse.html"
    assert target.is_file()
    return target, target.read_text(encoding="utf-8")


def test_browse_has_no_external_cdn_references(tmp_path: Path) -> None:
    _, html_text = _generate_browse_html(tmp_path)

    # AC5: grep for http:// or https:// anywhere in the document.
    assert _EXTERNAL_URL_RE.search(html_text) is None, (
        "browse.html contains an external URL — it must be airgap-safe"
    )


def test_browse_structural_airgap(tmp_path: Path) -> None:
    _, html_text = _generate_browse_html(tmp_path)

    parser = _TagCollector()
    parser.feed(html_text)

    assert parser.table_present, "browse.html must render a <table>"
    assert parser.external_src_or_href == [], (
        f"browse.html references external resources: {parser.external_src_or_href!r}"
    )


def test_browse_embeds_snapshot_entries_inline(tmp_path: Path) -> None:
    _, html_text = _generate_browse_html(tmp_path)

    # AC4: the snapshot data must appear in the HTML itself.
    # (1) Task ids are rendered into <td> cells for JS-disabled viewers.
    for entry in SYNTHETIC_ENTRIES:
        assert str(entry["task_id"]) in html_text
        assert str(entry["config"]) in html_text

    # (2) The JSON island survives intact. Extract it and validate shape.
    match = re.search(
        r'<script id="snapshot-data" type="application/json">(.*?)</script>',
        html_text,
        re.DOTALL,
    )
    assert match is not None, "browse.html must embed a snapshot-data JSON island"
    raw = match.group(1)
    # Reverse the </-escape we apply at render time before parsing.
    recovered = raw.replace("<\\/", "</")
    doc = json.loads(recovered)
    assert doc["columns"]
    assert len(doc["entries"]) == len(SYNTHETIC_ENTRIES)


def test_browse_renders_without_javascript(tmp_path: Path) -> None:
    """Graceful degradation: the table is present in static HTML."""
    _, html_text = _generate_browse_html(tmp_path)

    # A <noscript> note is present AND the <table> is outside any <script>.
    assert "<noscript>" in html_text
    # Crude check: the table comes before the <script> blocks in the doc.
    table_idx = html_text.index("<table")
    first_script_idx = html_text.index("<script")
    assert table_idx < first_script_idx, (
        "table must be rendered in static HTML before any <script> tags"
    )
