#!/usr/bin/env python3
"""Self-serve acceptance harness — the exit gate for codeprobe releases.

Proves, from nothing but a built wheel in a fresh venv, that the promise
codeprobe-f7rl exists to keep actually holds: a customer can
``pip install codeprobe``, install the packaged skills, mine a repo,
run an MCP-vs-baseline A/B comparison with repeats, and get an honest HTML
report — and that the four honesty refusals actually refuse. Every
assertion reads the JSON envelope's structured ``ok``/``error.code`` /
``data`` fields; nothing greps human-readable text (matched-complete-
telemetry principle).

POSITIVE JOURNEY:
    build wheel -> fresh venv -> pip install (assert no editable/checkout
    leakage) -> pip install the stub agent -> codeprobe skills install
    (assert 5 skills) -> codeprobe mine --no-llm (assert task_count >= 2)
    -> two-arm experiment on the same agent, differing only in mcp_config,
    --repeats 2 -> codeprobe run (assert worktree isolation: customer
    checkout untouched) -> codeprobe interpret --format html (assert the
    report exists and carries cost-provenance text for every summary
    figure).

NEGATIVE CASES (each asserts an exit code AND a machine-readable envelope
field, never free-text):
    (a) dirty checkout -> refused (DIRTY_CHECKOUT), then --allow-dirty
        proceeds.
    (b) unsupported language -> fails fast (UNSUPPORTED_LANGUAGE), zero
        tasks written.
    (c) incomparable arms (disjoint task sets) -> interpret's comparison
        is REFUSED (comparable=false, non-empty refusal_reason).
    (d) --no-llm makes zero non-loopback network calls, verified by a
        sitecustomize.py socket guard installed into the venv.

Usage:
    python scripts/e2e/self_serve_acceptance.py
    python scripts/e2e/self_serve_acceptance.py --keep-workdir  # for debugging

Exit codes:
    0   every assertion passed
    1   an assertion failed — see stderr for which one and why
    2   CLI / IO / subprocess error unrelated to the assertions themselves
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "e2e"))

from make_fixture_repo import (  # noqa: E402
    make_python_fixture_repo,
    make_ruby_fixture_repo,
)

_DEFAULT_TIMEOUT = 300


class HarnessAssertionError(RuntimeError):
    """Raised when a harness assertion fails — always caught and reported."""


def _log(msg: str) -> None:
    print(f"[e2e] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Subprocess + envelope helpers
# ---------------------------------------------------------------------------


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    _log(f"$ {' '.join(cmd)}" + (f"  (cwd={cwd})" if cwd else ""))
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        # Plain failure for git/pip/build/etc — always an infra error here.
        # codeprobe's own "handled failure" exit codes (1/2, parsed from a
        # well-formed envelope) are a separate, narrower exemption that
        # only `_run_codeprobe` applies (via check=False + its own
        # explicit 0/1/2 check below) — this generic helper must not mask
        # a real git/pip/build failure as if it were one.
        raise HarnessAssertionError(
            f"unexpected exit code {result.returncode} for: {' '.join(cmd)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _parse_envelope(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """Parse the single-line JSON envelope from *stdout* — never stderr."""
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    if not lines:
        raise HarnessAssertionError(
            f"expected a JSON envelope on stdout, got nothing.\n"
            f"stderr:\n{result.stderr}"
        )
    try:
        envelope = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise HarnessAssertionError(
            f"stdout's last line is not valid JSON: {lines[-1]!r} ({exc})"
        ) from exc
    if envelope.get("record_type") != "envelope":
        raise HarnessAssertionError(f"not a codeprobe envelope: {envelope!r}")
    return envelope


def _run_codeprobe(
    codeprobe_bin: Path,
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run ``codeprobe <args> --json`` and return the parsed envelope.

    Never raises on a codeprobe-level failure (exit 1/2 with a well-formed
    envelope) — callers assert on ``envelope["ok"]`` themselves. Raises
    :class:`HarnessAssertionError` only on a genuinely unexpected outcome (crash,
    unparseable stdout).
    """
    result = _run(
        [str(codeprobe_bin), *args, "--json"],
        cwd=cwd,
        env=env,
        timeout=timeout,
        check=False,
    )
    if result.returncode not in (0, 1, 2):
        raise HarnessAssertionError(
            f"codeprobe {' '.join(args)} crashed (exit {result.returncode}), "
            f"not a handled envelope failure.\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return _parse_envelope(result)


def _assert_ok(envelope: dict[str, Any], ctx: str) -> dict[str, Any]:
    if not envelope.get("ok"):
        raise HarnessAssertionError(
            f"{ctx}: expected ok=true, got envelope={json.dumps(envelope, indent=2)}"
        )
    return envelope.get("data") or {}


def _assert_error_code(envelope: dict[str, Any], code: str, ctx: str) -> dict[str, Any]:
    if envelope.get("ok"):
        raise HarnessAssertionError(f"{ctx}: expected a refusal, got ok=true: {envelope}")
    error = envelope.get("error") or {}
    actual = error.get("code")
    if actual != code:
        raise HarnessAssertionError(
            f"{ctx}: expected error.code={code!r}, got {actual!r} "
            f"(full envelope: {json.dumps(envelope, indent=2)})"
        )
    return error


# ---------------------------------------------------------------------------
# Setup: build wheel, fresh venv, install codeprobe + the stub agent
# ---------------------------------------------------------------------------


def build_wheel(dist_dir: Path) -> Path:
    _log("building wheel from repo source")
    _run([sys.executable, "-m", "build", "--wheel", "--outdir", str(dist_dir)], cwd=REPO_ROOT)
    wheels = sorted(dist_dir.glob("codeprobe-*.whl"))
    if len(wheels) != 1:
        raise HarnessAssertionError(f"expected exactly one wheel, found {wheels}")
    return wheels[0]


def create_venv(venv_dir: Path) -> tuple[Path, Path]:
    _log(f"creating fresh venv at {venv_dir}")
    venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
    return venv_dir / "bin" / "python", venv_dir / "bin" / "pip"


def assert_wheel_excludes_e2e_infra(wheel_path: Path) -> None:
    """The stub agent and scripts/e2e/ must never ship inside the wheel.

    Relies today on pyproject.toml's [tool.setuptools.packages.find]
    where=["src"] never regressing to sweep either in — exactly the kind
    of packaging-config drift this harness exists to catch, so it must be
    checked against the actual built artifact, not just asserted in a
    docstring.
    """
    with zipfile.ZipFile(wheel_path) as zf:
        names = zf.namelist()
    leaked = [n for n in names if "e2e_stub_agent" in n or n.startswith("scripts/")]
    if leaked:
        raise HarnessAssertionError(
            f"wheel {wheel_path.name} leaked E2E test infrastructure that "
            f"must never ship to customers: {leaked}"
        )


def install_wheel_and_assert_clean(pip_bin: Path, wheel_path: Path) -> None:
    _log("installing wheel into the fresh venv")
    assert_wheel_excludes_e2e_infra(wheel_path)
    _run([str(pip_bin), "install", str(wheel_path)])

    show = _run([str(pip_bin), "show", "-f", "codeprobe"])
    listed_files = show.stdout
    if "skills_data" not in listed_files or "SKILL.md" not in listed_files:
        raise HarnessAssertionError(
            "pip show -f codeprobe does not list any skills_data/*/SKILL.md "
            "package-data files — either the wheel didn't ship the skills, "
            "or this is somehow an editable/checkout install rather than "
            "the built wheel.\n" + listed_files
        )
    # An editable install's RECORD/direct_url.json points back at the repo
    # checkout; the built wheel's does not. Confirm explicitly rather than
    # inferring it from the (necessarily present, either way) skills lines
    # above.
    if str(REPO_ROOT) in listed_files:
        raise HarnessAssertionError(
            "pip show -f codeprobe output references the repo checkout path "
            f"({REPO_ROOT}) — this looks like an editable install leaking "
            "through, not the built wheel.\n" + listed_files
        )


def install_stub_agent(pip_bin: Path) -> None:
    _log("installing the e2e stub agent package")
    stub_agent_dir = REPO_ROOT / "tests" / "fixtures" / "e2e_stub_agent"
    try:
        _run([str(pip_bin), "install", str(stub_agent_dir)])
    finally:
        # Even a non-editable `pip install <path>` leaves setuptools build
        # residue (build/, *.egg-info/) IN the source directory being
        # installed from — confirmed empirically, not just in theory. In CI
        # that's harmless (the checkout is discarded after the job), but this
        # harness is also meant to run in a contributor's own dev checkout
        # (the acceptance criterion is "passes locally from a clean
        # checkout"), and leaving that residue there corrupts every
        # subsequent `pytest tests/` run in that checkout: any test file
        # that puts this directory on sys.path (or a stray future `pip
        # install -e` here) makes importlib.metadata.entry_points() pick up
        # the stray egg-info and silently register "e2e-stub" as a real
        # agent, which is exactly what broke
        # tests/test_cli.py::test_run_help_agent_lists_codex_from_registry
        # while authoring this harness. In a `finally` (not a bare trailing
        # statement) so a failed/timed-out install still cleans up whatever
        # partial residue pip left behind before the harness aborts.
        for stray in (stub_agent_dir / "build", *stub_agent_dir.glob("*.egg-info")):
            shutil.rmtree(stray, ignore_errors=True)


# ---------------------------------------------------------------------------
# Positive journey
# ---------------------------------------------------------------------------


def positive_journey(codeprobe_bin: Path, workdir: Path) -> None:
    fixture = workdir / "fixture-positive"
    _log("generating the Python fixture repo")
    make_python_fixture_repo(fixture)

    _log("codeprobe skills install")
    skills_dir = fixture / ".claude" / "skills"
    data = _assert_ok(
        _run_codeprobe(
            codeprobe_bin, ["skills", "install", "--dest", str(skills_dir)]
        ),
        "skills install",
    )
    installed = list(skills_dir.glob("codeprobe-*/SKILL.md"))
    if len(installed) != 5:
        raise HarnessAssertionError(
            f"expected 5 installed skills, found {len(installed)}: {installed} "
            f"(envelope data: {data})"
        )

    # A real customer commits installed skills alongside their repo (the
    # same way this very repo tracks .claude/skills/). Do the same here —
    # .claude/ is not on codeprobe's own dirty-checkout allowlist
    # (_is_codeprobe_owned only covers .codeprobe/, runs/, and
    # .codeprobe-worktrees*/), so leaving it uncommitted would make the
    # fixture dirty before `codeprobe run` ever gets called, for a reason
    # that has nothing to do with worktree isolation.
    _run(["git", "-C", str(fixture), "add", ".claude"])
    _run(["git", "-C", str(fixture), "commit", "-q", "-m", "chore: install codeprobe skills"])

    _log("codeprobe mine --no-llm")
    data = _assert_ok(
        _run_codeprobe(
            codeprobe_bin,
            [
                "mine",
                str(fixture),
                "--no-interactive",
                "--goal",
                "quality",
                "--count",
                "5",
                "--no-llm",
            ],
        ),
        "mine",
    )
    if data.get("task_count", 0) < 2:
        raise HarnessAssertionError(f"mine yielded task_count={data.get('task_count')} < 2")
    if data.get("llm_spend", {}).get("calls", -1) != 0:
        raise HarnessAssertionError(f"--no-llm mine still made LLM calls: {data.get('llm_spend')}")

    _log("experiment add-config: two arms, same agent, differing only in mcp_config")
    _assert_ok(
        _run_codeprobe(
            codeprobe_bin,
            [
                "experiment",
                "add-config",
                str(fixture),
                "--label",
                "armA",
                "--agent",
                "e2e-stub",
                "--mcp-config",
                '{"mcpServers": {"fakeserver": {"command": "true"}}}',
                "--mcp-mode",
                "loose",
            ],
        ),
        "add-config armA",
    )
    _assert_ok(
        _run_codeprobe(
            codeprobe_bin,
            ["experiment", "add-config", str(fixture), "--label", "armB", "--agent", "e2e-stub"],
        ),
        "add-config armB",
    )

    _log("codeprobe run --repeats 2 --uncontained")
    run_data = _assert_ok(
        _run_codeprobe(codeprobe_bin, ["run", str(fixture), "--repeats", "2", "--uncontained"]),
        "run",
    )
    if run_data.get("total_tasks", 0) < 4:
        raise HarnessAssertionError(f"run reported too few total_tasks: {run_data}")

    _log("asserting worktree isolation: customer checkout is untouched")
    status = _run(["git", "-C", str(fixture), "status", "--porcelain"])
    if status.stdout.strip():
        raise HarnessAssertionError(
            "customer checkout is dirty after `codeprobe run` — worktree "
            f"isolation leaked:\n{status.stdout}"
        )

    _log("codeprobe interpret --format html")
    interpret_data = _assert_ok(
        _run_codeprobe(codeprobe_bin, ["interpret", str(fixture), "--format", "html"]),
        "interpret",
    )
    html_path = interpret_data.get("html_report_path")
    if not html_path or not Path(html_path).is_file():
        raise HarnessAssertionError(f"html_report_path missing or not a file: {html_path!r}")
    html_text = Path(html_path).read_text(encoding="utf-8")
    summaries = interpret_data.get("report", {}).get("summaries", [])
    if not summaries:
        raise HarnessAssertionError("interpret envelope has no per-arm summaries to check")
    for summary in summaries:
        if summary.get("total_cost_usd") is None:
            raise HarnessAssertionError(
                f"arm {summary.get('label')!r} has total_cost_usd=None — the "
                "HTML report cannot carry cost provenance for a null total"
            )
    # The provenance text itself (see analysis/report.py::_cost_source_breakdown):
    # every arm here used the stub adapter's cost_source="unavailable", which
    # renders as the literal fallback string "unknown source" once at least
    # one cost value was captured (total_cost_usd is not None, asserted above).
    if "unknown source" not in html_text and "ci-metric-label" not in html_text:
        raise HarnessAssertionError(
            "HTML report contains no cost-provenance markup "
            "('unknown source' / ci-metric-label span) for any summary figure"
        )

    # report.summaries is populated per-arm independently of whether the
    # pairwise comparison itself was comparable — cost provenance renders
    # identically either way. Without this check, a regression that quietly
    # dropped mining yield to the harness's own >=2-task floor (below
    # stats.py's _MIN_PAIRED_TASKS=3) would make compare_configs REFUSE the
    # verdict, and this journey would still report PASS despite no longer
    # demonstrating the thing it exists to prove: a real, comparable
    # MCP-vs-baseline result.
    comparisons = interpret_data.get("report", {}).get("comparisons", [])
    if not comparisons:
        raise HarnessAssertionError("interpret envelope has no comparisons to check")
    comparison = comparisons[0]
    if comparison.get("comparable") is not True:
        raise HarnessAssertionError(
            f"positive journey's own A/B comparison is not comparable: {comparison}"
        )
    if not comparison.get("winner"):
        raise HarnessAssertionError(f"comparable=true but winner is empty: {comparison}")

    _log("positive journey: PASS")


# ---------------------------------------------------------------------------
# Negative case (a): dirty checkout
# ---------------------------------------------------------------------------


def negative_dirty_checkout(codeprobe_bin: Path, workdir: Path) -> None:
    _log("negative case (a): dirty checkout")
    fixture = workdir / "fixture-dirty"
    make_python_fixture_repo(fixture, dirty=True)

    _assert_ok(
        _run_codeprobe(
            codeprobe_bin,
            ["mine", str(fixture), "--no-interactive", "--goal", "quality", "--count", "3", "--no-llm"],
        ),
        "mine (dirty fixture)",
    )
    _assert_ok(
        _run_codeprobe(
            codeprobe_bin,
            ["experiment", "add-config", str(fixture), "--label", "solo", "--agent", "e2e-stub"],
        ),
        "add-config (dirty fixture)",
    )

    refused = _run_codeprobe(codeprobe_bin, ["run", str(fixture), "--uncontained"])
    _assert_error_code(refused, "DIRTY_CHECKOUT", "run on a dirty checkout")

    overridden = _run_codeprobe(
        codeprobe_bin, ["run", str(fixture), "--uncontained", "--allow-dirty"]
    )
    _assert_ok(overridden, "run on a dirty checkout with --allow-dirty")

    _log("negative case (a): PASS")


# ---------------------------------------------------------------------------
# Negative case (b): unsupported language
# ---------------------------------------------------------------------------


def negative_unsupported_language(codeprobe_bin: Path, workdir: Path) -> None:
    _log("negative case (b): unsupported language")
    fixture = workdir / "fixture-ruby"
    make_ruby_fixture_repo(fixture)

    envelope = _run_codeprobe(
        codeprobe_bin,
        ["mine", str(fixture), "--no-interactive", "--goal", "quality", "--count", "3", "--no-llm"],
    )
    _assert_error_code(envelope, "UNSUPPORTED_LANGUAGE", "mine on a ruby-only repo")

    tasks_dir = fixture / ".codeprobe" / "tasks"
    if tasks_dir.is_dir() and any(tasks_dir.iterdir()):
        raise HarnessAssertionError(
            f"UNSUPPORTED_LANGUAGE refusal still wrote tasks to {tasks_dir}"
        )

    _log("negative case (b): PASS")


# ---------------------------------------------------------------------------
# Negative case (c): incomparable arms (disjoint task sets)
# ---------------------------------------------------------------------------


def negative_incomparable_arms(venv_python: Path, codeprobe_bin: Path, workdir: Path) -> None:
    _log("negative case (c): incomparable arms")
    fixture = workdir / "fixture-incomparable"
    make_python_fixture_repo(fixture)

    mine_data = _assert_ok(
        _run_codeprobe(
            codeprobe_bin,
            ["mine", str(fixture), "--no-interactive", "--goal", "quality", "--count", "5", "--no-llm"],
        ),
        "mine (incomparable fixture)",
    )
    _assert_ok(
        _run_codeprobe(
            codeprobe_bin,
            ["experiment", "add-config", str(fixture), "--label", "armA", "--agent", "e2e-stub"],
        ),
        "add-config armA (incomparable)",
    )
    _assert_ok(
        _run_codeprobe(
            codeprobe_bin,
            ["experiment", "add-config", str(fixture), "--label", "armB", "--agent", "e2e-stub"],
        ),
        "add-config armB (incomparable)",
    )
    _assert_ok(
        _run_codeprobe(codeprobe_bin, ["run", str(fixture), "--uncontained"]),
        "run (incomparable, pre-split)",
    )

    task_ids = mine_data.get("task_count")
    exp_dir = fixture / ".codeprobe"
    all_task_ids = json.loads((exp_dir / "experiment.json").read_text())["task_ids"]
    if len(all_task_ids) < 4:
        raise HarnessAssertionError(
            f"need >=4 mined tasks to split into two non-trivial disjoint "
            f"subsets, got {len(all_task_ids)} (task_count={task_ids})"
        )
    midpoint = len(all_task_ids) // 2
    keep_a = all_task_ids[:midpoint]
    keep_b = all_task_ids[midpoint:]

    _run(
        [
            str(venv_python),
            str(REPO_ROOT / "scripts" / "e2e" / "split_disjoint_results.py"),
            str(exp_dir),
            "--config-a",
            "armA",
            "--keep-a",
            ",".join(keep_a),
            "--config-b",
            "armB",
            "--keep-b",
            ",".join(keep_b),
        ]
    )

    interpret_data = _assert_ok(
        _run_codeprobe(codeprobe_bin, ["interpret", str(fixture)]), "interpret (disjoint arms)"
    )
    comparisons = interpret_data.get("report", {}).get("comparisons", [])
    if not comparisons:
        raise HarnessAssertionError("interpret produced no comparisons to check")
    comparison = comparisons[0]
    if comparison.get("comparable") is not False:
        raise HarnessAssertionError(
            f"expected comparable=false for disjoint task sets, got: {comparison}"
        )
    if not comparison.get("refusal_reason"):
        raise HarnessAssertionError(f"comparable=false but refusal_reason is empty: {comparison}")

    _log("negative case (c): PASS")


# ---------------------------------------------------------------------------
# Negative case (d): --no-llm makes zero non-loopback network calls
# ---------------------------------------------------------------------------


def negative_no_llm_zero_network(
    venv_dir: Path, codeprobe_bin: Path, workdir: Path
) -> None:
    _log("negative case (d): --no-llm zero network calls")
    fixture = workdir / "fixture-network-guard"
    make_python_fixture_repo(fixture)

    site_packages_candidates = list(venv_dir.glob("lib/python3.*/site-packages"))
    if not site_packages_candidates:
        raise HarnessAssertionError(f"could not locate site-packages under {venv_dir}")
    site_packages = site_packages_candidates[0]
    guard_src = REPO_ROOT / "scripts" / "e2e" / "sitecustomize_template.py"
    shutil.copy(guard_src, site_packages / "sitecustomize.py")

    netlog = workdir / "netlog.txt"
    netlog.write_text("")
    env = {**os.environ, "CODEPROBE_E2E_NETLOG": str(netlog)}

    envelope = _run_codeprobe(
        codeprobe_bin,
        ["mine", str(fixture), "--no-interactive", "--goal", "quality", "--count", "3", "--no-llm"],
        env=env,
    )
    _assert_ok(envelope, "mine --no-llm under the network guard")

    attempts = netlog.read_text()
    if attempts.strip():
        raise HarnessAssertionError(
            f"--no-llm mine made non-loopback network attempt(s):\n{attempts}"
        )

    _log("negative case (d): PASS")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--keep-workdir",
        action="store_true",
        help="Do not delete the scratch directory on exit (for debugging).",
    )
    args = parser.parse_args(argv)

    # In CI, codeprobe refuses to auto-derive a tenant
    # (TENANT_REQUIRED_IN_CI, src/codeprobe/tenant.py). The harness IS a
    # controlled environment, so satisfy the guard the way its own error
    # message prescribes: set an explicit, deterministic tenant for every
    # subprocess. setdefault so a caller-provided tenant still wins.
    os.environ.setdefault("CODEPROBE_TENANT", "e2e-self-serve")

    workdir = Path(tempfile.mkdtemp(prefix="codeprobe-e2e-"))
    _log(f"scratch workdir: {workdir}")
    try:
        dist_dir = workdir / "dist"
        dist_dir.mkdir()
        wheel_path = build_wheel(dist_dir)

        venv_dir = workdir / "venv"
        venv_python, pip_bin = create_venv(venv_dir)
        install_wheel_and_assert_clean(pip_bin, wheel_path)
        install_stub_agent(pip_bin)
        codeprobe_bin = venv_dir / "bin" / "codeprobe"

        positive_journey(codeprobe_bin, workdir)
        negative_dirty_checkout(codeprobe_bin, workdir)
        negative_unsupported_language(codeprobe_bin, workdir)
        negative_incomparable_arms(venv_python, codeprobe_bin, workdir)
        # Installs a permanent socket guard into the venv — run last, since
        # nothing after this step needs network in that venv again.
        negative_no_llm_zero_network(venv_dir, codeprobe_bin, workdir)

        _log("ALL CHECKS PASSED")
        return 0
    except HarnessAssertionError as exc:
        _log(f"ASSERTION FAILED: {exc}")
        return 1
    except subprocess.TimeoutExpired as exc:
        _log(f"TIMEOUT: {exc}")
        return 2
    finally:
        if args.keep_workdir:
            _log(f"--keep-workdir: leaving {workdir} in place")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
