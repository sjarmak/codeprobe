#!/usr/bin/env python3
"""Pre-tag release readiness check over acceptance verdict history.

Runs every mechanical precondition from ``docs/release.md`` that must hold
BEFORE ``git tag v<version>``:

1. **Verdict history is green.** The two newest ``verdict-NNNN.json`` files
   in the history directory (written by ``scripts/acceptance_loop.py``) must
   both satisfy :meth:`acceptance.release.ReleaseGate.check_ready` —
   ``status == "EVALUATED"`` and ``all_pass is True``.
2. **Both of those two verdicts came from ``eval_mode=full``.**
   A default-mode green is NOT release evidence for mode-gated tiers: in
   default mode the mode-gated criteria are excluded from the evaluated
   denominator, so a tier can report 100% while evaluating nothing (see the
   acceptance-loop doctrine skill). A single full-mode green preceded (or
   followed) by a default-mode green is exactly the "one green can be luck"
   case the two-consecutive-green rule exists to prevent — it must be
   rejected here the same way ``ConvergenceController.is_release_ready``
   rejects it (both newest verdicts must share ``eval_mode``, and that mode
   must be ``full``). Verdicts written before ``eval_mode`` was recorded
   count as not-full.
3. **Both of those two verdicts record a REAL ``producer_agent``.**
   ``scripts/acceptance_loop.py`` stamps ``producer_agent`` into every
   full-mode verdict. A stub producer (``e2e-stub``) emits honest-but-fake
   telemetry (``cost_source="unavailable"``, ``cost_usd=0.0``) that
   satisfies the ``TELEM-*`` / ``SILENT-RUN-RESULTS-002`` statistical
   criteria without any genuine cost signal, so two ``e2e-stub`` full-mode
   greens would otherwise clear check 2 above and reach an irreversible
   PyPI tag having exercised nothing real (see ``docs/release.md``'s
   "Producer agent" precondition). A verdict whose ``producer_agent`` is
   missing, ``None``, or in the known dry/non-real set is rejected.
4. **No critical/high-severity criterion is handler-less in either of the
   two newest verdicts.** A ``check_type`` with no registered handler in
   ``acceptance.verify.Verifier._handlers()`` is structurally unevaluable in
   EVERY eval mode (``skip_reason="no_handler"``), excluded from the
   evaluated denominator same as an eval_mode skip — so a critical criterion
   can go unchecked forever while the verdict still reports EVALUATED +
   all_pass. Medium/low severity handler gaps are reported as a warning,
   not a failure (mirrors ``BLOCKING_SEVERITIES`` in
   ``acceptance/converge.py``).
5. **CHANGELOG.md has a ``## <version>`` heading** for the version in
   ``pyproject.toml``.
6. **The version is not already tagged** — ``v<version>`` must not exist,
   i.e. the version bump landed.

Every failed check prints the exact command that fixes it and the script
exits nonzero. All checks are deterministic structural comparisons — no
heuristics, no semantic judgment.

Usage:
    python scripts/pre_tag_check.py
    python scripts/pre_tag_check.py --history-dir acceptance/verdict-history

Exit codes:
    0   ready to tag
    1   one or more preconditions failed
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT_DEFAULT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT_DEFAULT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT_DEFAULT))

from acceptance.release import ReleaseGate  # noqa: E402

#: Must match the naming scheme scripts/acceptance_loop.py writes.
_VERDICT_FILE_RE = re.compile(r"^verdict-(\d{4})\.json$")

_LOOP_CMD = "uv run python scripts/acceptance_loop.py --eval-mode full --iterations 2"


def find_newest_verdicts(history_dir: Path, count: int = 2) -> list[Path]:
    """Return the ``count`` newest verdict files, ordered oldest → newest."""
    if not history_dir.is_dir():
        return []
    matching = [p for p in history_dir.iterdir() if _VERDICT_FILE_RE.match(p.name)]
    return sorted(matching, key=lambda p: p.name)[-count:]


def read_pyproject_version(pyproject_path: Path) -> str:
    with pyproject_path.open("rb") as fh:
        data = tomllib.load(fh)
    version = (data.get("project") or {}).get("version")
    if not isinstance(version, str) or not version:
        raise ValueError(f"{pyproject_path} has no [project].version string")
    return version


def changelog_has_heading(changelog_path: Path, version: str) -> bool:
    if not changelog_path.is_file():
        return False
    heading = re.compile(rf"^##\s+{re.escape(version)}\b", re.MULTILINE)
    return heading.search(changelog_path.read_text()) is not None


def tag_exists(repo_root: Path, tag: str) -> bool:
    """Return True iff git reports ``tag`` as an existing tag."""
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "tag", "--list", tag],
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(completed.stdout.strip())


def _verdict_eval_mode(path: Path) -> str | None:
    """Read ``eval_mode`` from a verdict file; None on any parse problem."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    mode = data.get("eval_mode")
    return mode if isinstance(mode, str) else None


#: Known non-real / dry producer_agent values. Narrow and explicit on
#: purpose — this must reject the stub without ever misclassifying a real
#: backend (the registered ``codeprobe.agents`` entry points: claude, codex,
#: copilot). Extend deliberately if a new dry/stub producer is introduced.
_DRY_PRODUCER_AGENTS = frozenset({"e2e-stub"})


def _verdict_producer_agent(path: Path) -> str | None:
    """Read ``producer_agent`` from a verdict file; None on any parse
    problem, missing key, or if the verdict predates this field."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    agent = data.get("producer_agent")
    return agent if isinstance(agent, str) else None


#: Severities that must never be structurally unevaluable at tag time —
#: mirrors acceptance/converge.py's BLOCKING_SEVERITIES for quarantine.
_BLOCKING_NO_HANDLER_SEVERITIES = frozenset({"critical", "high"})


def _no_handler_criteria(path: Path) -> list[dict[str, str]]:
    """Read ``no_handler_criteria`` from a verdict file; ``[]`` on any parse
    problem or if the verdict predates this field."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    entries = data.get("no_handler_criteria")
    return entries if isinstance(entries, list) else []


def run_checks(repo_root: Path, history_dir: Path) -> int:
    """Run all preconditions; print PASS/FAIL per check; return exit code."""
    failures: list[str] = []
    version = read_pyproject_version(repo_root / "pyproject.toml")
    print(f"pre-tag check for version {version}")

    # 1. Two green verdicts.
    verdict_paths = find_newest_verdicts(history_dir)
    if len(verdict_paths) < 2:
        failures.append(
            f"verdict history at {history_dir} has {len(verdict_paths)} "
            f"verdict file(s); two are required.\n"
            f"  Fix: run the acceptance loop first:\n    {_LOOP_CMD}"
        )
    elif not ReleaseGate(repo_root).check_ready(verdict_paths):
        details = []
        for path in verdict_paths:
            try:
                data = json.loads(path.read_text())
                details.append(
                    f"{path.name}: status={data.get('status')!r} "
                    f"all_pass={data.get('all_pass')!r}"
                )
            except (OSError, json.JSONDecodeError) as exc:
                details.append(f"{path.name}: unreadable ({exc})")
        failures.append(
            "the two newest verdicts are not both EVALUATED + all_pass:\n  "
            + "\n  ".join(details)
            + "\n  Fix: resolve the failures the verdicts report, then re-run:\n"
            f"    {_LOOP_CMD}"
        )
    else:
        print(f"PASS: last two verdicts EVALUATED + all_pass ({history_dir})")

    # 2. Both of the two newest verdicts must be eval_mode=full — mirrors
    #    ConvergenceController.is_release_ready's same-mode-and-full
    #    requirement. A single full-mode green (mixed with a default-mode
    #    green, in either order) is NOT sufficient: the mode-gated criteria
    #    were only ever evaluated once, which is exactly the "one green can
    #    be luck" case the two-consecutive-green rule exists to prevent.
    if len(verdict_paths) >= 2:
        modes = [_verdict_eval_mode(p) for p in verdict_paths]
        if modes[0] != "full" or modes[1] != "full":
            failures.append(
                f"the two newest verdicts are not both eval_mode=full "
                f"(modes: {modes}). A default-mode green is NOT release "
                "evidence for mode-gated tiers — their criteria are excluded "
                "from the evaluated denominator in default mode — and a "
                "single full-mode green mixed with a default-mode green is "
                "NOT release evidence either: the mode-gated criteria set "
                "was only ever evaluated once, the exact 'one green can be "
                "luck' case the two-consecutive-green rule exists to "
                "prevent.\n"
                f"  Fix: {_LOOP_CMD}"
            )
        else:
            print("PASS: both of the last two verdicts are eval_mode=full")

    # 3. Both of the two newest verdicts must record a REAL producer_agent —
    #    never a known dry/non-real stand-in (e2e-stub) or missing/None. A
    #    stub producer's honest-but-fake telemetry (cost_source=
    #    "unavailable", cost_usd=0.0) satisfies the TELEM-* /
    #    SILENT-RUN-RESULTS-002 statistical criteria without any genuine
    #    cost signal, so two e2e-stub full-mode greens would otherwise clear
    #    check 2 above and reach an irreversible PyPI tag having exercised
    #    nothing real.
    if len(verdict_paths) >= 2:
        producer_agents = [_verdict_producer_agent(p) for p in verdict_paths]
        bad_producers = {
            path.name: agent
            for path, agent in zip(verdict_paths, producer_agents, strict=True)
            if agent is None or agent in _DRY_PRODUCER_AGENTS
        }
        if bad_producers:
            failures.append(
                "the two newest verdicts do not both record a real "
                f"producer_agent (dry/missing: {bad_producers}). A verdict "
                "produced by a non-real agent (or with no producer_agent "
                "recorded) is NOT release evidence — its statistical "
                "criteria may be satisfied by honest-but-fake telemetry "
                "rather than a genuine agent run.\n"
                f"  Fix: {_LOOP_CMD} --target-repo <a real repo> "
                "--producer-agent claude (or another real registered "
                "codeprobe.agents backend)"
            )
        else:
            print(
                "PASS: both of the last two verdicts record a real "
                f"producer_agent ({producer_agents})"
            )

    # 4. No critical/high-severity criterion is structurally unevaluable in
    #    every mode (no registered Verifier handler for its check_type).
    #    These are excluded from evaluated_pct same as eval_mode skips, so a
    #    verdict can be EVALUATED + all_pass while a critical criterion was
    #    never checked at all — never mind which eval mode ran.
    if len(verdict_paths) >= 2:
        blocking: dict[str, set[str]] = {}
        non_blocking: dict[str, set[str]] = {}
        for path in verdict_paths:
            for entry in _no_handler_criteria(path):
                cid = entry.get("criterion_id", "?")
                severity = entry.get("severity", "?")
                bucket = (
                    blocking
                    if severity in _BLOCKING_NO_HANDLER_SEVERITIES
                    else non_blocking
                )
                bucket.setdefault(cid, set()).add(severity)
        if blocking:
            ids = sorted(blocking)
            failures.append(
                f"{len(ids)} critical/high-severity criterion(ia) have no "
                f"registered Verifier handler and were never evaluated in "
                f"EITHER of the last two verdicts: {ids}.\n"
                "  Fix: register a handler for their check_type in "
                "acceptance/verify.py::Verifier._handlers(), or downgrade "
                "severity with human sign-off if the check truly doesn't "
                "warrant blocking release."
            )
        elif non_blocking:
            print(
                f"WARNING: {len(non_blocking)} medium/low-severity "
                f"criterion(ia) have no registered Verifier handler and "
                f"were never evaluated: {sorted(non_blocking)} (not "
                "blocking — severity is below critical/high)."
            )
        else:
            print("PASS: no critical/high-severity criterion is handler-less")

    # 4. Changelog heading.
    if not changelog_has_heading(repo_root / "CHANGELOG.md", version):
        failures.append(
            f"CHANGELOG.md has no '## {version}' heading.\n"
            "  Fix: move the Unreleased content under a new "
            f"'## {version}' heading and commit it with the version bump."
        )
    else:
        print(f"PASS: CHANGELOG.md has a '## {version}' heading")

    # 5. Tag does not already exist.
    tag = f"v{version}"
    if tag_exists(repo_root, tag):
        failures.append(
            f"tag {tag} already exists — pyproject.toml still says "
            f"{version}, so the version bump has not landed.\n"
            "  Fix: bump [project].version in pyproject.toml "
            "(ReleaseGate.bump_version) and commit it with the CHANGELOG edit."
        )
    else:
        print(f"PASS: tag {tag} does not exist yet")

    sys.stdout.flush()
    if failures:
        print(f"\nNOT READY to tag {tag}:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print(f"\nREADY to tag {tag}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check every docs/release.md precondition before tagging."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT_DEFAULT,
        help="Repo root (default: parent of scripts/).",
    )
    parser.add_argument(
        "--history-dir",
        type=Path,
        default=None,
        help=(
            "Verdict history directory (default: "
            "<repo-root>/acceptance/verdict-history)."
        ),
    )
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    history_dir = (
        args.history_dir.resolve()
        if args.history_dir is not None
        else repo_root / "acceptance" / "verdict-history"
    )
    return run_checks(repo_root, history_dir)


if __name__ == "__main__":
    sys.exit(main())
