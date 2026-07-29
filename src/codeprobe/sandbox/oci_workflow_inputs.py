"""Validate OCI publication workflow inputs before writing outputs."""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Final, NoReturn

from codeprobe.sandbox.oci_references import (
    is_qualified_registry_host,
    validate_image_reference,
)

EXPECTED_IMAGES: Final[tuple[str, ...]] = ("codeprobe-agent", "codeprobe-scoring")
_SHA_RE: Final[re.Pattern[str]] = re.compile(r"[a-f0-9]{40}\Z")


class WorkflowInputError(RuntimeError):
    """Raised when workflow-provided inputs are unsafe or invalid."""


class _WorkflowArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise WorkflowInputError("workflow arguments are invalid")


def validate_registry_inputs(registry: str, namespace: str) -> None:
    _reject_control_chars("registry", registry)
    _reject_control_chars("namespace", namespace)
    if not registry or "/" in registry or not is_qualified_registry_host(registry):
        raise WorkflowInputError("registry host is invalid")
    if registry.lower() != registry:
        raise WorkflowInputError("registry host is invalid")
    if not namespace or namespace.startswith("/") or namespace.endswith("/"):
        raise WorkflowInputError("repository namespace is invalid")
    if "//" in namespace:
        raise WorkflowInputError("repository namespace is invalid")
    try:
        _safe_validate_ref(
            "workflow registry sentinel",
            f"{registry}/{namespace.lower()}/codeprobe-release-pair:sentinel",
        )
    except WorkflowInputError as exc:
        raise WorkflowInputError("registry or namespace is invalid") from exc


def validate_credentials(username: str, password: str) -> None:
    _reject_control_chars("registry username", username)
    _reject_control_chars("registry password", password)
    if not username or not password:
        raise WorkflowInputError("registry credentials are required")


def image_refs(
    registry: str,
    namespace: str,
    image: str,
    version: str,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    validate_registry_inputs(registry, namespace)
    if image not in EXPECTED_IMAGES:
        raise WorkflowInputError("image name is unsupported")
    raw = f"{registry}/{namespace}/{image}".lower()
    version_ref = _safe_validate_ref("version image ref", f"{raw}:{version}")
    candidate_tag = _candidate_tag(version, os.environ if env is None else env)
    candidate_ref = _safe_validate_ref("candidate image ref", f"{raw}:{candidate_tag}")
    return {"name": raw, "version_ref": version_ref, "candidate_ref": candidate_ref}


def _safe_validate_ref(name: str, ref_name: str) -> str:
    try:
        return validate_image_reference(name, ref_name)
    except ValueError as exc:
        raise WorkflowInputError("OCI image reference is invalid") from exc


def resolve_credentials(
    registry: str, namespace: str, env: Mapping[str, str] | None = None
) -> dict[str, str]:
    validate_registry_inputs(registry, namespace)
    present = os.environ if env is None else env
    username = present.get("REGISTRY_USERNAME", "")
    password = present.get("REGISTRY_PASSWORD", "")
    if registry == "ghcr.io":
        custom_pair = bool(username), bool(password)
        if custom_pair == (False, False):
            username = _required_env(present, "GITHUB_ACTOR")
            password = _required_env(present, "DEFAULT_GITHUB_TOKEN")
        elif custom_pair != (True, True):
            raise WorkflowInputError("custom registry credential pair is incomplete")
    if registry != "ghcr.io" and (not username or not password):
        raise WorkflowInputError("non-default registry credentials are required")
    validate_credentials(username, password)
    return {"username": username, "password": password}


def _candidate_tag(version: str, env: Mapping[str, str]) -> str:
    release_sha = _required_env(env, "RELEASE_SHA")
    run_id = _required_env(env, "GITHUB_RUN_ID")
    run_attempt = _required_env(env, "GITHUB_RUN_ATTEMPT")
    if _SHA_RE.fullmatch(release_sha) is None:
        raise WorkflowInputError("release sha is invalid")
    if not run_id.isdigit() or not run_attempt.isdigit():
        raise WorkflowInputError("workflow run identity is invalid")
    return f"{version}-{run_id}-{run_attempt}-{release_sha[:12]}"


def _required_env(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if value is None or value == "":
        raise WorkflowInputError("required workflow environment is missing")
    _reject_control_chars("workflow environment", value)
    return value


def _reject_control_chars(label: str, value: str) -> None:
    if "\r" in value or "\n" in value:
        raise WorkflowInputError(f"{label} must not contain control characters")


def _write_outputs(values: dict[str, str], output_path: Path) -> None:
    with output_path.open("a", encoding="utf-8") as output:
        for key, value in values.items():
            print(f"{key}={value}", file=output)


def _mask_secret(value: str) -> None:
    print(f"::add-mask::{_escape_workflow_command_data(value)}")


def _escape_workflow_command_data(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _build_parser() -> argparse.ArgumentParser:
    parser = _WorkflowArgumentParser(description="Validate OCI workflow inputs.")
    subcommands = parser.add_subparsers(
        dest="command", parser_class=_WorkflowArgumentParser, required=True
    )
    image = subcommands.add_parser("image-refs")
    image.add_argument("--registry", required=True)
    image.add_argument("--namespace", required=True)
    image.add_argument("--image", required=True)
    image.add_argument("--version", required=True)
    image.add_argument("--github-output", type=Path, required=True)
    creds = subcommands.add_parser("credentials")
    creds.add_argument("--registry", required=True)
    creds.add_argument("--namespace", required=True)
    creds.add_argument("--github-output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _build_parser().parse_args(argv)
        if args.command == "image-refs":
            values = image_refs(args.registry, args.namespace, args.image, args.version)
        else:
            values = resolve_credentials(args.registry, args.namespace)
            _mask_secret(values["password"])
        _write_outputs(values, args.github_output)
    except WorkflowInputError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (KeyError, ValueError, OSError):
        print("workflow input validation failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
