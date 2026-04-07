"""Sourcegraph ground truth enrichment for MCP-advantaged task families.

Calls the Sourcegraph ``find_references`` MCP endpoint to discover files that
reference a symbol through aliased imports, re-exports, and indirect usage
that local grep misses.  The enriched file set is unioned with the grep-based
ground truth so the oracle scores agents fairly when they use code intelligence.

ZFC compliant: pure IO (HTTP call) + mechanical path extraction + deterministic
set arithmetic.  No semantic judgment.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)


def enrich_ground_truth(
    symbol: str,
    defining_file: str,
    grep_files: frozenset[str],
    repo_sg_name: str,
    sg_token: str,
    sg_url: str = "https://demo.sourcegraph.com",
) -> tuple[frozenset[str], dict[str, str]]:
    """Call Sourcegraph find_references, return (all_files, tier_map).

    *tier_map* assigns each file one of:
    - ``"required"``  — found by grep (regardless of whether SG also found it)
    - ``"supplementary"`` — found only by Sourcegraph

    On any API failure the function gracefully degrades to grep-only results.

    Args:
        symbol: The symbol name to search for references.
        defining_file: Repo-relative path where the symbol is defined.
        grep_files: Files already found by local grep.
        repo_sg_name: Sourcegraph repo identifier, e.g.
            ``"github.com/sg-evals/numpy"``.
        sg_token: Sourcegraph access token (never logged).
        sg_url: Sourcegraph instance URL.

    Returns:
        A tuple of ``(all_files, tier_map)`` where *all_files* is the union of
        grep and SG results, and *tier_map* maps each file to its tier.
    """
    sg_files = _call_find_references(
        symbol=symbol,
        defining_file=defining_file,
        repo_sg_name=repo_sg_name,
        sg_token=sg_token,
        sg_url=sg_url,
    )

    if sg_files is None:
        # API failure — fall back to grep-only
        tier_map = {f: "required" for f in grep_files}
        return grep_files, tier_map

    all_files = grep_files | sg_files
    tier_map: dict[str, str] = {}
    for f in all_files:
        if f in grep_files:
            tier_map[f] = "required"
        else:
            tier_map[f] = "supplementary"

    return frozenset(all_files), tier_map


def _call_find_references(
    *,
    symbol: str,
    defining_file: str,
    repo_sg_name: str,
    sg_token: str,
    sg_url: str,
) -> frozenset[str] | None:
    """Call Sourcegraph ``sg_find_references`` via Streamable HTTP MCP transport.

    Returns a frozenset of repo-relative file paths, or None on failure.
    """
    import json as _json

    url = f"{sg_url.rstrip('/')}/.api/mcp/v1"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "sg_find_references",
            "arguments": {
                "repo": repo_sg_name,
                "path": defining_file,
                "symbol": symbol,
            },
        },
    }
    headers = {
        "Authorization": f"token {sg_token}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=30,
            stream=True,
        )
        resp.raise_for_status()
        return _parse_sse_references(resp, repo_sg_name)
    except Exception:
        # Log without leaking the token value
        logger.warning(
            "Sourcegraph find_references failed for %s in %s (repo: %s)",
            symbol,
            defining_file,
            repo_sg_name,
        )
        return None


def _parse_sse_references(
    resp: requests.Response,
    repo_sg_name: str,
) -> frozenset[str]:
    """Parse SSE event stream from Sourcegraph MCP and extract file paths."""
    import json as _json
    import re

    paths: set[str] = set()
    # Pattern: "# repo <separator> file" header lines in the text content.
    # The separator is typically an en-dash (–), arrow (→), or similar Unicode.
    header_re = re.compile(
        r"^#\s+" + re.escape(repo_sg_name) + r"\s+\S+\s+(.+)$",
        re.MULTILINE,
    )

    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        data = _json.loads(line[6:])

        # Check for JSON-RPC error
        if "error" in data:
            return frozenset()

        content = data.get("result", {}).get("content", [])
        for item in content:
            text = item.get("text", "")
            if not text:
                continue
            for match in header_re.finditer(text):
                paths.add(match.group(1).strip())
        break  # Only one SSE data event expected

    return frozenset(paths)
