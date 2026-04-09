"""Allow ``python -m codeprobe.core`` to dispatch scoring CLI."""

from __future__ import annotations

from codeprobe.core.scoring import _cli_main

if __name__ == "__main__":
    _cli_main()
