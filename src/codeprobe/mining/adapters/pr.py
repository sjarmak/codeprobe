"""PR narrative adapter — fetches merged-PR body text via the GitHub CLI."""

from __future__ import annotations

import json as _json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from codeprobe.mining.sources import NarrativeBundle

logger = logging.getLogger(__name__)

_GH_TIMEOUT = 15
_MAX_BODY_CHARS = 50_000


@dataclass(frozen=True)
class PRAdapter:
    """Fetch a merged PR's body/title/labels for a given commit SHA.

    Shells out to ``gh pr list --search <sha>``. Returns ``None`` when
    ``gh`` is unavailable, the search yields no hits, or the matching PR
    has an empty body (so the caller can fall through to another adapter
    in the user-selected chain).
    """

    name: str = field(default="pr")

    def fetch(self, repo: Path, commit_sha: str) -> NarrativeBundle | None:
        try:
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--state",
                    "merged",
                    "--search",
                    commit_sha,
                    "--json",
                    "number,title,body,labels",
                ],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=_GH_TIMEOUT,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("PRAdapter: gh unavailable for %s: %s", commit_sha[:8], exc)
            return None

        if result.returncode != 0:
            logger.debug(
                "PRAdapter: gh pr list failed for %s: %s",
                commit_sha[:8],
                result.stderr.strip(),
            )
            return None

        try:
            items = _json.loads(result.stdout or "[]")
        except _json.JSONDecodeError:
            logger.debug("PRAdapter: gh returned invalid JSON for %s", commit_sha[:8])
            return None

        if not isinstance(items, list) or not items:
            return None

        if len(items) > 1:
            # Selection behavior is unchanged (first match wins), but surface
            # the ambiguity so callers can see which PRs matched the SHA.
            numbers = [
                item.get("number", "?")
                for item in items
                if isinstance(item, dict)
            ]
            logger.warning(
                "PRAdapter: %d PRs matched sha %s (selecting first); "
                "matched PR numbers=%s",
                len(items),
                commit_sha[:8],
                numbers,
            )

        pr = items[0]
        body = (pr.get("body") or "").strip()
        if not body:
            return None

        labels_raw = pr.get("labels") or []
        labels: list[str] = []
        if isinstance(labels_raw, list):
            for lbl in labels_raw:
                if isinstance(lbl, dict) and "name" in lbl:
                    labels.append(str(lbl["name"]))

        metadata: dict[str, str] = {
            "pr_number": str(pr.get("number", "")),
            "title": str(pr.get("title", "")),
        }
        if labels:
            metadata["labels"] = ",".join(labels)

        return NarrativeBundle(
            text=body[:_MAX_BODY_CHARS],
            metadata=metadata,
            source_name=self.name,
        )
