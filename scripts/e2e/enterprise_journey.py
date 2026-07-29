#!/usr/bin/env python3
"""Run the release-blocking clean-wheel enterprise journey."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.e2e.enterprise_artifacts import (  # noqa: E402
    EnterpriseHarnessError,
    assert_no_secret_values,
    build_evidence_request,
    validate_image_labels,
)
from scripts.e2e.enterprise_install import (  # noqa: E402
    install_candidate_wheel,
    installed_version,
    write_agent_probe_shim,
    write_fake_private_registry_engine,
)
from scripts.e2e.enterprise_runtime import (  # noqa: E402
    JourneyRuntime,
    base_environment,
    envelope_data,
)
from scripts.e2e.make_fixture_repo import make_python_fixture_repo  # noqa: E402

_DIGEST_REFERENCE: Final[re.Pattern[str]] = re.compile(
    r"[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}\Z"
)
_COMMIT: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{40}\Z")
_PROXY_SECRET: Final[str] = "fixture-proxy-password"
_PROXY_URL: Final[str] = f"http://fixture-user:{_PROXY_SECRET}@127.0.0.1:9"
_PRIVATE_AGENT_DIGEST: Final[str] = "sha256:" + "a" * 64
_PRIVATE_SCORING_DIGEST: Final[str] = "sha256:" + "b" * 64
_CLAUDE_CREDENTIAL_ENVS: Final[frozenset[str]] = frozenset(
    {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}
)
_JOURNEY_STEPS: Final[tuple[str, ...]] = (
    "install-wheel",
    "bootstrap",
    "doctor",
    "assess",
    "mine",
    "run",
    "interpret",
    "evidence-preview",
    "evidence-export",
    "evidence-validate",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--candidate-version", required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--credential-env", required=True)
    parser.add_argument("--agent-image", required=True)
    parser.add_argument("--scoring-image", required=True)
    parser.add_argument("--max-cost-usd", required=True, type=float)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--keep-workdir", action="store_true")
    return parser


def _validate_args(args: argparse.Namespace) -> str:
    if args.agent == "e2e-stub" or not args.agent.strip():
        raise EnterpriseHarnessError("a real release agent is required")
    if args.agent != "claude" or args.credential_env not in _CLAUDE_CREDENTIAL_ENVS:
        raise EnterpriseHarnessError("release agent credential mapping is unsupported")
    if args.max_cost_usd <= 0 or not _COMMIT.fullmatch(args.candidate_commit):
        raise EnterpriseHarnessError("candidate identity or budget is invalid")
    for image in (args.agent_image, args.scoring_image):
        if not _DIGEST_REFERENCE.fullmatch(image):
            raise EnterpriseHarnessError("release images must be digest-pinned")
    credential = os.environ.get("CODEPROBE_RELEASE_AGENT_CREDENTIAL", "")
    if len(credential) < 8:
        raise EnterpriseHarnessError("release agent credential is unavailable")
    return credential


def _prepare_runtime(
    args: argparse.Namespace,
    workdir: Path,
    credential: str,
) -> tuple[Path, Path, JourneyRuntime, dict[str, str], Path]:
    source_log = workdir / "source-reads.log"
    python, codeprobe = install_candidate_wheel(
        args.wheel.resolve(),
        workdir / "venv",
        source_root=REPO_ROOT,
        source_read_log=source_log,
    )
    version, package_path = installed_version(python)
    if version != args.candidate_version or package_path.is_relative_to(REPO_ROOT):
        raise EnterpriseHarnessError("installed candidate identity does not match")
    shim_bin = workdir / "agent-bin"
    shim_bin.mkdir()
    write_agent_probe_shim(
        shim_bin,
        agent=args.agent,
        agent_image=args.agent_image,
    )
    home = workdir / "home"
    home.mkdir()
    env = base_environment(
        home=home,
        shim_bin=shim_bin,
        config_path=workdir / "container-images.json",
        credential_env=args.credential_env,
        credential_value=credential,
        agent_image=args.agent_image,
        scoring_image=args.scoring_image,
    )
    return python, codeprobe, JourneyRuntime(
        codeprobe,
        env=env,
        timeout=args.timeout,
    ), env, source_log


def _bootstrap(
    runtime: JourneyRuntime,
    args: argparse.Namespace,
    workdir: Path,
) -> None:
    runtime.codeprobe(
        "bootstrap",
        [
            "bootstrap",
            "--engine",
            "docker",
            "--agent-image",
            args.agent_image,
            "--scoring-image",
            args.scoring_image,
        ],
        cwd=workdir,
    )
    for image in (args.agent_image, args.scoring_image):
        result = runtime.external(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .Config.Labels}}",
                image,
            ],
            cwd=workdir,
        )
        try:
            labels = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise EnterpriseHarnessError("published image labels are malformed") from exc
        if not isinstance(labels, dict):
            raise EnterpriseHarnessError("published image labels are malformed")
        validate_image_labels(
            labels,
            version=args.candidate_version,
            commit=args.candidate_commit,
        )


def _make_private_ca(runtime: JourneyRuntime, workdir: Path) -> Path:
    key = workdir / "fixture-ca.key"
    cert = workdir / "fixture-ca.pem"
    runtime.external(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=codeprobe-enterprise-fixture",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        cwd=workdir,
    )
    key.unlink()
    return cert


def _doctor_and_assess(
    runtime: JourneyRuntime,
    args: argparse.Namespace,
    fixture: Path,
    ca_path: Path,
) -> None:
    home = Path(runtime.environment["HOME"])
    runtime.codeprobe(
        "skills-install",
        ["skills", "install", "--dest", str(home / ".claude" / "skills")],
        cwd=fixture,
    )
    proxy_env = {
        "HTTP_PROXY": _PROXY_URL,
        "HTTPS_PROXY": _PROXY_URL,
        "NO_PROXY": "127.0.0.1,localhost",
        "SSL_CERT_FILE": str(ca_path),
    }
    runtime.codeprobe(
        "doctor",
        [
            "doctor",
            "--repo",
            str(fixture),
            "--agent",
            args.agent,
            "--private-ca",
            str(ca_path),
        ],
        cwd=fixture,
        env=proxy_env,
    )
    runtime.codeprobe("assess", ["assess", str(fixture)], cwd=fixture)


def _mine_and_configure(
    runtime: JourneyRuntime,
    args: argparse.Namespace,
    fixture: Path,
) -> None:
    mine = envelope_data(
        runtime.codeprobe(
            "mine",
            [
                "mine",
                str(fixture),
                "--no-interactive",
                "--goal",
                "quality",
                "--count",
                "1",
                "--no-llm",
            ],
            cwd=fixture,
        ),
        "mine",
    )
    if mine.get("task_count") != 1:
        raise EnterpriseHarnessError("mine did not produce exactly one synthetic task")
    for label in ("A", "B"):
        runtime.codeprobe(
            f"configure-{label}",
            [
                "experiment",
                "add-config",
                str(fixture),
                "--label",
                label,
                "--agent",
                args.agent,
            ],
            cwd=fixture,
        )


def _run_and_interpret(
    runtime: JourneyRuntime,
    args: argparse.Namespace,
    fixture: Path,
) -> dict[str, Any]:
    runtime.codeprobe(
        "run",
        [
            "run",
            str(fixture),
            "--repeats",
            "1",
            "--parallel",
            "1",
            "--config-parallel",
            "1",
            "--max-turns",
            "3",
            "--timeout",
            "600",
            "--max-cost-usd",
            str(args.max_cost_usd),
        ],
        cwd=fixture,
    )
    if _git_status(fixture):
        raise EnterpriseHarnessError("worktree isolation left the fixture dirty")
    data = envelope_data(
        runtime.codeprobe(
            "interpret",
            ["interpret", str(fixture), "--format", "html"],
            cwd=fixture,
        ),
        "interpret",
    )
    html = data.get("html_report_path")
    if not isinstance(html, str) or not Path(html).resolve().is_relative_to(fixture):
        raise EnterpriseHarnessError("interpret output location is invalid")
    if not Path(html).is_file():
        raise EnterpriseHarnessError("interpret report was not written")
    report = data.get("report")
    if not isinstance(report, dict):
        raise EnterpriseHarnessError("interpret report is malformed")
    return data


def _evidence_round_trip(
    runtime: JourneyRuntime,
    args: argparse.Namespace,
    fixture: Path,
    interpret_data: dict[str, Any],
    workdir: Path,
) -> tuple[Path, str]:
    experiment_path = fixture / ".codeprobe" / "experiment.json"
    experiment = json.loads(experiment_path.read_text(encoding="utf-8"))
    request = build_evidence_request(
        report=interpret_data["report"],
        experiment=experiment,
        candidate_version=args.candidate_version,
    )
    request_path = workdir / "evidence-request.json"
    request_path.write_text(json.dumps(request, sort_keys=True), encoding="utf-8")
    digest = _preview_request(runtime, request_path, fixture)
    _export_and_validate(runtime, request_path, fixture, workdir, digest)
    return request_path, digest


def _preview_request(
    runtime: JourneyRuntime,
    request_path: Path,
    fixture: Path,
) -> str:
    preview = envelope_data(
        runtime.codeprobe(
            "evidence-preview",
            ["snapshot", "evidence", "preview", str(request_path)],
            cwd=fixture,
        ),
        "evidence-preview",
    )
    digest = preview.get("approval_digest")
    if not isinstance(digest, str):
        raise EnterpriseHarnessError("evidence preview has no approval digest")
    return digest


def _export_and_validate(
    runtime: JourneyRuntime,
    request_path: Path,
    fixture: Path,
    workdir: Path,
    digest: str,
) -> None:
    _assert_approval_mismatch(runtime, request_path, fixture, workdir)
    bundle = workdir / "evidence-bundle"
    runtime.codeprobe(
        "evidence-export",
        [
            "snapshot",
            "evidence",
            "export",
            str(request_path),
            "--out",
            str(bundle),
            "--approve",
            digest,
        ],
        cwd=fixture,
    )
    validated = envelope_data(
        runtime.codeprobe(
            "evidence-validate",
            [
                "snapshot",
                "evidence",
                "validate",
                str(bundle),
                "--expect",
                digest,
            ],
            cwd=fixture,
        ),
        "evidence-validate",
    )
    if validated.get("approval_digest") != digest:
        raise EnterpriseHarnessError("receiving-side evidence digest is not bound")


def _assert_approval_mismatch(
    runtime: JourneyRuntime,
    request_path: Path,
    fixture: Path,
    workdir: Path,
) -> None:
    runtime.expect_codeprobe_error(
        [
            "snapshot",
            "evidence",
            "export",
            str(request_path),
            "--out",
            str(workdir / "rejected-bundle"),
            "--approve",
            "sha256:" + "0" * 64,
        ],
        cwd=fixture,
        expected_code="EVIDENCE_APPROVAL_MISMATCH",
    )


def _network_variants(
    runtime: JourneyRuntime,
    python: Path,
    workdir: Path,
    ca_path: Path,
) -> list[dict[str, object]]:
    fake_bin = workdir / "private-engine-bin"
    fake_bin.mkdir()
    engine_log = workdir / "private-engine.jsonl"
    write_fake_private_registry_engine(fake_bin, engine_log)
    private_env = {
        "PATH": f"{fake_bin}{os.pathsep}{runtime.environment['PATH']}",
        "CODEPROBE_CONTAINER_CONFIG": str(workdir / "private-images.json"),
        "CODEPROBE_PRIVATE_ENGINE_LOG": str(engine_log),
        "HTTPS_PROXY": _PROXY_URL,
        "SSL_CERT_FILE": str(ca_path),
    }
    runtime.codeprobe(
        "private-registry-bootstrap",
        [
            "bootstrap",
            "--engine",
            "docker",
            "--agent-image",
            f"registry.enterprise.invalid/platform/codeprobe-agent@{_PRIVATE_AGENT_DIGEST}",
            "--scoring-image",
            (
                "registry.enterprise.invalid/platform/codeprobe-scoring@"
                f"{_PRIVATE_SCORING_DIGEST}"
            ),
        ],
        cwd=workdir,
        env=private_env,
    )
    _validate_private_engine_log(engine_log)
    _verify_offline_guard(runtime, python, workdir)
    return [
        {
            "name": "proxy-private-ca",
            "status": "passed",
            "public_network_attempts": 0,
        },
        {
            "name": "offline-private-registry",
            "status": "passed",
            "public_network_attempts": 0,
        },
    ]


def _validate_private_engine_log(path: Path) -> None:
    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    if not records or any(
        not record.get("proxy")
        or not record.get("private_ca")
        or "registry.enterprise.invalid" not in " ".join(record.get("args", []))
        or "docker.io" in " ".join(record.get("args", []))
        or "ghcr.io" in " ".join(record.get("args", []))
        for record in records
    ):
        raise EnterpriseHarnessError("private-registry network fixture failed")


def _verify_offline_guard(
    runtime: JourneyRuntime,
    python: Path,
    workdir: Path,
) -> None:
    script = (
        "from codeprobe.cli.errors import DiagnosticError;"
        "from codeprobe.net import guard_offline;"
        "\ntry: guard_offline('enterprise-public-fixture')"
        "\nexcept DiagnosticError as exc:"
        "\n assert exc.code == 'OFFLINE_NET_ATTEMPT'"
        "\nelse: raise SystemExit(2)"
    )
    runtime.external(
        [str(python), "-c", script],
        cwd=workdir,
        env={"CODEPROBE_OFFLINE": "1"},
    )


def _artifact_paths(workdir: Path, fixture: Path) -> list[Path]:
    roots = (
        fixture / ".codeprobe",
        workdir / "evidence-request.json",
        workdir / "evidence-bundle",
        workdir / "container-images.json",
        workdir / "private-images.json",
        workdir / "private-engine.jsonl",
    )
    paths: list[Path] = []
    for root in roots:
        if root.is_file() and not root.is_symlink():
            paths.append(root)
        elif root.is_dir():
            paths.extend(
                path for path in root.rglob("*")
                if path.is_file() and not path.is_symlink()
            )
    return paths


def _journey_evidence(
    args: argparse.Namespace,
    *,
    interpret_data: dict[str, Any],
    network_variants: list[dict[str, object]],
    source_reads: int,
    secret_count: int,
) -> dict[str, Any]:
    observed_cost = sum(
        float(summary["total_cost_usd"])
        for summary in interpret_data["report"]["summaries"]
    )
    return {
        "schema_version": "codeprobe.enterprise-journey.v1",
        "candidate": {
            "version": args.candidate_version,
            "commit": args.candidate_commit,
            "wheel_sha256": _sha256(args.wheel),
            "agent_image": args.agent_image,
            "scoring_image": args.scoring_image,
        },
        "producer": {"agent": args.agent, "kind": "real"},
        "budget": {
            "max_cost_usd": args.max_cost_usd,
            "observed_cost_usd": observed_cost,
        },
        "steps": [
            {"name": name, "status": "passed"} for name in _JOURNEY_STEPS
        ],
        "invariants": {
            "candidate_version_matches": True,
            "container_isolation": True,
            "evidence_digest_bound": True,
            "output_locations_valid": True,
            "source_checkout_reads": source_reads,
            "structured_errors": True,
            "worktree_isolation": True,
        },
        "network_variants": network_variants,
        "secret_scan": {"values_checked": secret_count, "leaks": 0},
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_status(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_result(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(path)


def _run(args: argparse.Namespace, credential: str, workdir: Path) -> None:
    python, _, runtime, _, source_log = _prepare_runtime(args, workdir, credential)
    fixture = make_python_fixture_repo(workdir / "synthetic-repo", feature_count=1)
    _bootstrap(runtime, args, workdir)
    ca_path = _make_private_ca(runtime, workdir)
    _doctor_and_assess(runtime, args, fixture, ca_path)
    _mine_and_configure(runtime, args, fixture)
    interpret_data = _run_and_interpret(runtime, args, fixture)
    _evidence_round_trip(runtime, args, fixture, interpret_data, workdir)
    variants = _network_variants(runtime, python, workdir, ca_path)
    source_reads = len(source_log.read_text(encoding="utf-8").splitlines())
    secret_count = assert_no_secret_values(
        _artifact_paths(workdir, fixture),
        runtime.outputs,
        [credential, _PROXY_SECRET],
    )
    evidence = _journey_evidence(
        args,
        interpret_data=interpret_data,
        network_variants=variants,
        source_reads=source_reads,
        secret_count=secret_count,
    )
    _write_result(args.out.resolve(), evidence)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    workdir = Path(tempfile.mkdtemp(prefix="codeprobe-enterprise-"))
    try:
        credential = _validate_args(args)
        _run(args, credential, workdir)
    except (EnterpriseHarnessError, OSError, ValueError, subprocess.SubprocessError):
        print("enterprise journey failed; see the failing step", file=sys.stderr)
        return 1
    finally:
        if args.keep_workdir:
            print(f"enterprise journey workdir retained at {workdir}", file=sys.stderr)
        else:
            shutil.rmtree(workdir, ignore_errors=True)
    print(f"enterprise journey evidence written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
