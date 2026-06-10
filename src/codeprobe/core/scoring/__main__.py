"""CLI dispatch for ``python -m codeprobe.core.scoring --artifact <task_dir>``."""

from __future__ import annotations

from codeprobe.core.scoring.scorers import _cli_main

if __name__ == "__main__":
    _cli_main()
