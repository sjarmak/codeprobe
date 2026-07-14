"""``codeprobe snapshot`` — produce shareable snapshots with a safe default.

Subcommands:

- ``snapshot create`` — emit a ``SNAPSHOT.json`` (and optionally a redacted
  body tree) from an experiment directory.
- ``snapshot verify`` — recompute hashes and verify the signed attestation.

The default redaction mode is ``hashes-only`` and is the only mode this CLI
accepts without the explicit ``--allow-source-in-export`` opt-in.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from codeprobe.cli._error_handler import CodeprobeGroup
from codeprobe.cli._output_helpers import (
    add_json_flags,
    emit_envelope,
    resolve_mode,
)
from codeprobe.cli._tenant import resolve_tenant, tenant_option
from codeprobe.cli.errors import DiagnosticError, PrescriptiveError
from codeprobe.snapshot.canary import (
    CANARY_DEFAULT,
    CanaryFailedError,
    CanaryGate,
    load_canary_proof,
)
from codeprobe.snapshot.create import (
    FairnessLeakError,
    SymlinkEscapeError,
    create_snapshot,
)
from codeprobe.snapshot.exporters import (
    export_browse,
    export_datadog,
    export_sheets,
    export_sigma,
)
from codeprobe.snapshot.fairness import (
    check_fairness,
    write_fairness_report,
)
from codeprobe.snapshot.redact import (
    PUBLISHABLE_DEFAULT,
    RedactionMode,
)
from codeprobe.snapshot.scanners import (
    GitleaksScanner,
    PatternScanner,
    Scanner,
    ScannerUnavailableError,
    TrufflehogScanner,
)
from codeprobe.snapshot.verify import verify_snapshot_extended

_VALID_MODES: tuple[RedactionMode, ...] = ("hashes-only", "contents", "secrets")


@click.group(cls=CodeprobeGroup)
def snapshot() -> None:
    """Create and verify shareable snapshots of experiment directories."""


def _build_scanner(name: str) -> Scanner:
    """Resolve a scanner by name.

    ``pattern`` is always available. ``gitleaks`` and ``trufflehog`` require
    the corresponding binary on PATH — scanning with them raises
    :class:`ScannerUnavailableError` at runtime if missing.
    """

    if name == "pattern":
        return PatternScanner()
    if name == "gitleaks":
        return GitleaksScanner()
    if name == "trufflehog":
        return TrufflehogScanner()
    raise PrescriptiveError(
        code="UNKNOWN_BACKEND",
        message=(
            f"unknown --scanner {name!r} (choose: pattern, gitleaks, trufflehog)"
        ),
        next_try_flag="--scanner",
        next_try_value="pattern",
    )


@snapshot.command("create")
@add_json_flags
@click.argument(
    "experiment_dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
@click.option(
    "--out",
    "out_path",
    required=True,
    type=click.Path(path_type=Path),
    help="Output directory for the snapshot (SNAPSHOT.json lives here).",
)
@click.option(
    "--redact",
    "mode",
    type=click.Choice(list(_VALID_MODES), case_sensitive=False),
    default=PUBLISHABLE_DEFAULT,
    show_default=True,
    help=(
        "Redaction mode. 'hashes-only' is the publishable default. "
        "'contents' and 'secrets' copy redacted bodies and require "
        "--allow-source-in-export."
    ),
)
@click.option(
    "--allow-source-in-export",
    is_flag=True,
    default=False,
    help=(
        "Required for --redact=contents or --redact=secrets. Acknowledges "
        "that the snapshot will contain file bodies (after scanner redaction)."
    ),
)
@click.option(
    "--scanner",
    "scanner_name",
    default="pattern",
    show_default=True,
    help="Scanner to use: pattern, gitleaks, trufflehog.",
)
@click.option(
    "--canary-proof",
    "canary_proof_path",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "Path to a pre-recorded canary proof JSON. Required for "
        "--redact=secrets in non-interactive contexts."
    ),
)
@click.option(
    "--signing-key",
    default=None,
    envvar="CODEPROBE_SIGNING_KEY",
    help=(
        "HMAC signing key for SNAPSHOT.json attestation. Falls back to "
        "CODEPROBE_SIGNING_KEY env var. When unset, manifest is written as "
        "'unsigned' (body hash only)."
    ),
)
@click.option(
    "--check-fairness/--no-check-fairness",
    "check_fairness_flag",
    default=False,
    show_default=True,
    help=(
        "Run the Class E fairness scan on tasks under EXPERIMENT_DIR before "
        "writing the snapshot. Refuses to publish if oracle paths leak into "
        "agent-facing files (CLAUDE.md, AGENTS.md, README, .cursor/rules)."
    ),
)
@click.option(
    "--fairness-repo-root",
    "fairness_repo_root",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help=(
        "Repository whose agent-facing files the fairness scan should "
        "examine. Defaults to EXPERIMENT_DIR. Has no effect without "
        "--check-fairness."
    ),
)
@tenant_option(required=False)
@click.pass_context
def create_cmd(
    ctx: click.Context,
    experiment_dir: Path,
    out_path: Path,
    mode: str,
    allow_source_in_export: bool,
    scanner_name: str,
    canary_proof_path: Path | None,
    signing_key: str | None,
    check_fairness_flag: bool,
    fairness_repo_root: Path | None,
    tenant_id: str | None,
    json_flag: bool,
    no_json_flag: bool,
    json_lines_flag: bool,
) -> None:
    """Create a snapshot of EXPERIMENT_DIR.

    By default the snapshot is metadata-only (sha256 + size per file). To
    export file bodies — even with redaction — pass ``--allow-source-in-export``
    alongside ``--redact=contents`` or ``--redact=secrets``.

    Secrets mode additionally requires a pre-publish canary proof: either
    supply ``--canary-proof <path>`` or run interactively so the CLI can
    prompt you to confirm the scanner caught a planted canary.
    """
    out_mode = resolve_mode(
        "snapshot create", json_flag, no_json_flag, json_lines_flag,
    )

    # Resolve tenant up-front so a CI misconfig fails fast and the
    # resolved id is always available for the final envelope.
    snap_tenant, snap_tenant_source = resolve_tenant(
        ctx,
        tenant_id,
        cwd=experiment_dir.resolve(),
        url_override=None,
    )

    # R4 tenant lock: serialize concurrent snapshot-create invocations
    # within the same tenant. verify/export are read-only / external-write
    # so they don't need the lock.
    from codeprobe.tenant_lock import acquire_tenant_lock

    _lock_cm = acquire_tenant_lock(snap_tenant or "local", "snapshot")
    _lock_cm.__enter__()
    try:
        mode_cast: RedactionMode = mode  # type: ignore[assignment]

        if mode_cast in ("contents", "secrets") and not allow_source_in_export:
            raise PrescriptiveError(
                code="SOURCE_EXPORT_REQUIRES_ACK",
                message=(
                    f"Refusing --redact={mode_cast} without --allow-source-in-export. "
                    "This flag is required to acknowledge that file bodies will be "
                    "written (after scanner redaction). See docs/SNAPSHOT_REDACTION.md."
                ),
                next_try_flag="--allow-source-in-export",
                next_try_value="",
                detail={"mode": mode_cast},
            )

        scanner: Scanner | None = None
        if mode_cast in ("contents", "secrets"):
            scanner = _build_scanner(scanner_name)

        canary_result = None
        if mode_cast == "secrets":
            if canary_proof_path is not None:
                canary_result = load_canary_proof(canary_proof_path)
                if not canary_result.passed:
                    raise DiagnosticError(
                        code="CANARY_PROOF_FAILED",
                        message=(
                            f"Canary proof at {canary_proof_path} is marked passed=False. "
                            "Refusing to create a secrets-mode snapshot."
                        ),
                        diagnose_cmd="codeprobe doctor",
                        terminal=True,
                        detail={"canary_proof_path": str(canary_proof_path)},
                    )
            else:
                if not sys.stdin.isatty():
                    raise PrescriptiveError(
                        code="CANARY_PROOF_REQUIRED",
                        message=(
                            "--redact=secrets requires --canary-proof <path> in "
                            "non-interactive contexts. Either provide the proof file "
                            "or run this command in a TTY so you can paste the canary "
                            "string interactively."
                        ),
                        next_try_flag="--canary-proof",
                        next_try_value="<path-to-canary-proof.json>",
                    )
                click.echo(
                    f"Interactive canary gate. Paste this canary to continue:\n"
                    f"  {CANARY_DEFAULT}"
                )
                pasted = click.prompt("canary", default="", show_default=False)
                if pasted.strip() != CANARY_DEFAULT:
                    raise DiagnosticError(
                        code="CANARY_MISMATCH",
                        message="Canary mismatch. Aborting.",
                        diagnose_cmd="codeprobe doctor",
                        terminal=True,
                    )
                assert scanner is not None
                try:
                    canary_result = CanaryGate(scanner).require_pass_or_raise()
                except CanaryFailedError as e:
                    raise DiagnosticError(
                        code="CANARY_GATE_FAILED",
                        message=str(e),
                        diagnose_cmd="codeprobe doctor --json",
                        terminal=True,
                    ) from e

        try:
            status = create_snapshot(
                experiment_dir=experiment_dir,
                out_dir=out_path,
                mode=mode_cast,
                scanner=scanner,
                signing_key=signing_key,
                canary_proof=canary_result,
                allow_source_in_export=allow_source_in_export,
                fairness_check=check_fairness_flag,
                fairness_repo_root=fairness_repo_root,
            )
        except FairnessLeakError as e:
            raise DiagnosticError(
                code="FAIRNESS_LEAK_DETECTED",
                message=f"Snapshot blocked: {e}",
                diagnose_cmd=(
                    f"codeprobe snapshot fairness {experiment_dir} "
                    "--report fairness.json"
                ),
                terminal=True,
                detail={"experiment_dir": str(experiment_dir)},
            ) from e
        except (
            PermissionError,
            CanaryFailedError,
            ScannerUnavailableError,
            FileNotFoundError,
            SymlinkEscapeError,
        ) as e:
            raise DiagnosticError(
                code="SNAPSHOT_CREATE_FAILED",
                message=f"Snapshot failed: {e}",
                diagnose_cmd="codeprobe doctor",
                terminal=True,
                detail={"experiment_dir": str(experiment_dir)},
            ) from e

        if out_mode.mode == "pretty":
            click.echo(json.dumps(status, indent=2))
        else:
            emit_envelope(
                command="snapshot create",
                data={
                    "status": status,
                    "tenant": snap_tenant,
                    "tenant_source": snap_tenant_source,
                },
            )
    finally:
        _lock_cm.__exit__(None, None, None)


@snapshot.command("verify")
@add_json_flags
@click.argument(
    "snapshot_dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
@click.option(
    "--signing-key",
    default=None,
    envvar="CODEPROBE_SIGNING_KEY",
    help="HMAC key (falls back to CODEPROBE_SIGNING_KEY). Required for hmac-signed manifests.",
)
def verify_cmd(
    snapshot_dir: Path,
    signing_key: str | None,
    json_flag: bool,
    no_json_flag: bool,
    json_lines_flag: bool,
) -> None:
    """Verify a snapshot's attestation, symlink containment, and file hashes."""
    mode = resolve_mode(
        "snapshot verify", json_flag, no_json_flag, json_lines_flag,
    )

    result = verify_snapshot_extended(snapshot_dir, signing_key=signing_key)
    payload = {
        "ok": result.ok,
        "reason": result.reason,
        "body_sha256_matches": result.base.body_sha256_matches,
        "signature_matches": result.base.signature_matches,
        "symlinks_contained": result.symlinks_contained,
        "file_hashes_match": result.file_hashes_match,
        "offending_paths": result.offending_paths,
    }
    if mode.mode == "pretty":
        click.echo(json.dumps(payload, indent=2))
    else:
        emit_envelope(
            command="snapshot verify",
            ok=result.ok,
            data=payload,
        )
    if not result.ok:
        raise DiagnosticError(
            code="SNAPSHOT_VERIFY_FAILED",
            message=(
                f"Snapshot verify failed: {result.reason or 'integrity mismatch'}"
            ),
            diagnose_cmd="codeprobe snapshot verify --verbose",
            terminal=True,
            detail=payload,
        )


@snapshot.command("fairness")
@add_json_flags
@click.argument(
    "task_root",
    nargs=-1,
    required=True,
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
@click.option(
    "--repo-root",
    "repo_root",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help=(
        "Repository whose agent-facing files (CLAUDE.md, AGENTS.md, README, "
        ".cursor/rules) should be scanned for oracle leaks. Defaults to "
        "TASK_ROOT."
    ),
)
@click.option(
    "--extra-file",
    "extra_files",
    multiple=True,
    type=click.Path(path_type=Path),
    help=(
        "Additional file to treat as agent-facing (e.g. an injected "
        "CLAUDE.md from the harness). May be repeated."
    ),
)
@click.option(
    "--check-preambles/--no-check-preambles",
    default=True,
    help=(
        "Also render the built-in preambles and check them for oracle "
        "leaks. Default: enabled."
    ),
)
@click.option(
    "--report",
    "report_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional path to write a JSON fairness report.",
)
def fairness_cmd(
    task_root: tuple[Path, ...],
    repo_root: Path | None,
    extra_files: tuple[Path, ...],
    check_preambles: bool,
    report_path: Path | None,
    json_flag: bool,
    no_json_flag: bool,
    json_lines_flag: bool,
) -> None:
    """Class E fairness scan: detect oracle leakage in agent-facing files.

    Walks every task directory under TASK_ROOT, extracts oracle tokens
    (file paths, symbols, scalar answers) from each task's
    ``ground_truth.json``, and searches every agent-facing file under
    ``--repo-root`` (CLAUDE.md, AGENTS.md, README, .cursor/rules/) for
    verbatim hits. Any hit means an agent reading that file gets an
    unfair hint relative to one that doesn't.

    Exits non-zero when leaks are found so this command can serve as a
    CI gate before publishing new tasks.
    """
    mode = resolve_mode(
        "snapshot fairness", json_flag, no_json_flag, json_lines_flag,
    )

    effective_repo_root = repo_root if repo_root is not None else task_root[0]

    rendered_preambles: dict[str, str] | None = None
    if check_preambles:
        rendered_preambles = _render_default_preambles()

    result = check_fairness(
        task_roots=list(task_root),
        repo_root=effective_repo_root,
        extra_agent_files=list(extra_files),
        rendered_preambles=rendered_preambles,
    )

    if report_path is not None:
        write_fairness_report(result, report_path)

    payload = result.to_dict()
    if mode.mode == "pretty":
        click.echo(json.dumps(payload, indent=2))
    else:
        emit_envelope(
            command="snapshot fairness",
            ok=result.ok,
            data=payload,
        )

    if not result.ok:
        raise DiagnosticError(
            code="FAIRNESS_LEAK_DETECTED",
            message=(
                f"Fairness scan found {len(result.leaks)} leak(s) across "
                f"{result.tasks_scanned} task(s). See report for details."
            ),
            diagnose_cmd="codeprobe snapshot fairness <task_root> --report fairness.json",
            terminal=True,
            detail={
                "task_root": [str(p) for p in task_root],
                "repo_root": str(effective_repo_root),
                "leak_count": len(result.leaks),
            },
        )


def _render_default_preambles() -> dict[str, str]:
    """Render the built-in preambles for the dynamic check.

    Uses a small, fixed capability + tool table covering the canonical
    code-intel surface. The point is to verify the preamble template
    itself doesn't bleed any task-specific content; the exact tools we
    pass are immaterial as long as the rendered text reflects the
    template's substitution behaviour.
    """
    from codeprobe.preambles import _PREAMBLES_DIR
    from codeprobe.preambles.generator import render_preamble

    rendered: dict[str, str] = {}
    # Static .md preambles: read raw text (no Jinja substitution).
    for md_path in sorted(_PREAMBLES_DIR.glob("*.md")):
        rendered[md_path.name] = md_path.read_text(encoding="utf-8")

    # Dynamic Jinja preamble: render with a representative capability set.
    # If anything goes wrong (capability-id drift, missing template), skip
    # this preamble rather than failing the whole scan — the static .md
    # files still get checked.
    try:
        text = render_preamble(
            capability_set=["KEYWORD_SEARCH", "SYMBOL_REFERENCES", "FILE_READ"],
            tool_table=[
                {
                    "name": "search_keyword",
                    "description": "Indexed keyword search across the repo.",
                    "capability_id": "KEYWORD_SEARCH",
                },
                {
                    "name": "find_references",
                    "description": "Find all references to a symbol.",
                    "capability_id": "SYMBOL_REFERENCES",
                },
                {
                    "name": "read_file",
                    "description": "Read a file by path.",
                    "capability_id": "FILE_READ",
                },
            ],
        )
        rendered["custom.md.j2"] = text
    except Exception:
        # The dynamic render is best-effort. The static .md preambles
        # above provide leakage coverage on their own.
        pass
    return rendered


_EXPORT_FORMATS: tuple[str, ...] = ("datadog", "sigma", "sheets", "browse")


@snapshot.command("export")
@add_json_flags
@click.argument(
    "snapshot_dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(list(_EXPORT_FORMATS), case_sensitive=False),
    required=True,
    help="Export format: datadog, sigma, sheets, or browse.",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "Output path. Defaults: datadog -> <snapshot>/datadog.json, "
        "sigma -> <snapshot>/ (emits sigma_results.csv + sigma_schema.json), "
        "sheets -> <snapshot>/sheets.tsv, browse -> <snapshot>/browse.html."
    ),
)
def export_cmd(
    snapshot_dir: Path,
    fmt: str,
    out_path: Path | None,
    json_flag: bool,
    no_json_flag: bool,
    json_lines_flag: bool,
) -> None:
    """Export SNAPSHOT_DIR into an observability artefact.

    The export subcommand is a pure local transform — no network calls are
    issued. Callers ship the generated artefact to the downstream system
    (Datadog intake, Sigma/dbt, Google Sheets, or a browser) themselves.
    """
    mode = resolve_mode(
        "snapshot export", json_flag, no_json_flag, json_lines_flag,
    )

    fmt_normalised = fmt.lower()

    try:
        if fmt_normalised == "datadog":
            target = out_path if out_path is not None else snapshot_dir / "datadog.json"
            written = export_datadog(snapshot_dir, target)
            payload = {"format": "datadog", "out": str(written)}
        elif fmt_normalised == "sigma":
            target_dir = out_path if out_path is not None else snapshot_dir
            csv_path, schema_path = export_sigma(snapshot_dir, target_dir)
            payload = {
                "format": "sigma",
                "csv": str(csv_path),
                "schema": str(schema_path),
            }
        elif fmt_normalised == "sheets":
            target = out_path if out_path is not None else snapshot_dir / "sheets.tsv"
            written = export_sheets(snapshot_dir, target)
            payload = {"format": "sheets", "out": str(written)}
        elif fmt_normalised == "browse":
            target = out_path if out_path is not None else snapshot_dir / "browse.html"
            written = export_browse(snapshot_dir, target)
            payload = {"format": "browse", "out": str(written)}
        else:  # pragma: no cover — Click choice guards this branch.
            raise PrescriptiveError(
                code="UNKNOWN_BACKEND",
                message=f"unknown --format {fmt!r}",
                next_try_flag="--format",
                next_try_value=_EXPORT_FORMATS[0],
            )
    except FileNotFoundError as e:
        raise DiagnosticError(
            code="METADATA_MISSING",
            message=f"Export failed: {e}",
            diagnose_cmd=f"codeprobe snapshot verify {snapshot_dir}",
            terminal=True,
            detail={"snapshot_dir": str(snapshot_dir), "format": fmt},
        ) from e

    if mode.mode == "pretty":
        click.echo(json.dumps(payload, indent=2))
    else:
        emit_envelope(command="snapshot export", data=payload)
