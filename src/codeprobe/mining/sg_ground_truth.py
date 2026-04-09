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
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from codeprobe.mining.multi_repo import FileRef

logger = logging.getLogger(__name__)


def enrich_ground_truth(
    symbol: str,
    defining_file: str,
    grep_files: frozenset[str],
    repo_sg_name: str,
    sg_url: str = "https://demo.sourcegraph.com",
) -> tuple[frozenset[str], dict[str, str]]:
    """Call Sourcegraph find_references, return (all_files, tier_map).

    *tier_map* assigns each file one of:
    - ``"required"``  — found by grep (regardless of whether SG also found it)
    - ``"supplementary"`` — found only by Sourcegraph

    On any API failure the function gracefully degrades to grep-only results.
    Authentication is resolved internally via :func:`sg_auth.get_valid_token`.

    Args:
        symbol: The symbol name to search for references.
        defining_file: Repo-relative path where the symbol is defined.
        grep_files: Files already found by local grep.
        repo_sg_name: Sourcegraph repo identifier, e.g.
            ``"github.com/sg-evals/numpy"``.
        sg_url: Sourcegraph instance URL.

    Returns:
        A tuple of ``(all_files, tier_map)`` where *all_files* is the union of
        grep and SG results, and *tier_map* maps each file to its tier.
    """
    sg_files = _call_find_references(
        symbol=symbol,
        defining_file=defining_file,
        repo_sg_name=repo_sg_name,
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
    sg_url: str,
) -> frozenset[str] | None:
    """Call Sourcegraph ``sg_find_references`` via Streamable HTTP MCP transport.

    Authentication is resolved via :func:`sg_auth.get_valid_token`.
    On a 401 response, the token is refreshed once and the request retried.

    Returns a frozenset of repo-relative file paths, or None on failure.
    """
    from codeprobe.mining.sg_auth import AuthError, get_valid_token

    try:
        cached = get_valid_token(sg_url)
    except AuthError:
        logger.warning(
            "No Sourcegraph auth available for %s — skipping find_references",
            sg_url,
        )
        return None

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

    for attempt in range(2):
        headers = {
            "Authorization": f"token {cached.access_token}",
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
            if resp.status_code == 401 and attempt == 0:
                try:
                    cached = get_valid_token(sg_url, force_refresh=True)
                    continue
                except AuthError:
                    logger.warning(
                        "Sourcegraph 401 for %s and refresh failed. "
                        "Run `codeprobe auth sourcegraph`.",
                        sg_url,
                    )
                    return None
            resp.raise_for_status()
            return _parse_sse_references(resp, repo_sg_name)
        except Exception:
            logger.warning(
                "Sourcegraph find_references failed for %s in %s (repo: %s)",
                symbol,
                defining_file,
                repo_sg_name,
            )
            return None
    return None


class SourcegraphSymbolResolver:
    """SymbolResolver implementation backed by Sourcegraph ``find_references``.

    Wraps :func:`_call_find_references` so cross-repo mining can use
    Sourcegraph's cross-repo code intelligence when available.

    This class intentionally mirrors the ``SymbolResolver`` Protocol from
    :mod:`codeprobe.mining.multi_repo` via duck typing — it does not import
    the Protocol to avoid a circular dependency.
    """

    def __init__(
        self,
        defining_file: str = "",
        sg_url: str = "https://demo.sourcegraph.com",
    ) -> None:
        self._defining_file = defining_file
        self._sg_url = sg_url

    def find_references(self, symbol: str, repos: list[str]) -> list[FileRef]:
        """Return cross-repo references for *symbol* across *repos*.

        *repos* are Sourcegraph repo identifiers (e.g.
        ``github.com/owner/name``), not local paths.
        """
        # Local import to avoid circular dependency with multi_repo module.
        from codeprobe.mining.multi_repo import FileRef

        refs: list[FileRef] = []
        for repo_sg_name in repos:
            paths = _call_find_references(
                symbol=symbol,
                defining_file=self._defining_file,
                repo_sg_name=repo_sg_name,
                sg_url=self._sg_url,
            )
            if paths is None:
                continue
            for path in paths:
                refs.append(FileRef(repo=repo_sg_name, path=path))
        return refs

    def resolve_symbol_at(self, repo: str, path: str, line: int) -> object | None:
        """Not implemented for Sourcegraph — returns None.

        Call-site resolution via Sourcegraph ``go_to_definition`` is a
        follow-up; callers should fall back to :class:`RipgrepResolver`.
        """
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
