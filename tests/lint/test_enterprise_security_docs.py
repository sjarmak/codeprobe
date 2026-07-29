"""Machine-check the enterprise security documentation inventory."""

from __future__ import annotations

import inspect
import json
import re
import tempfile
from collections.abc import Callable
from pathlib import Path

from click.testing import CliRunner

import codeprobe.cli.purge_cmd as purge_cmd
import codeprobe.cli.run_cmd as run_cmd
import codeprobe.net.credential_ttl as credential_ttl
from codeprobe.adapters import _base as adapter_base
from codeprobe.adapters import claude as claude_adapter
from codeprobe.cli import main
from codeprobe.cli.purge_cmd import _DISCLOSURE, _MCP_TEMPFILE_PATTERN
from codeprobe.core import sandbox as core_sandbox
from codeprobe.core.scoring import sandbox as scoring_sandbox
from codeprobe.net.offline import guard_offline
from codeprobe.sandbox import runner as sandbox_runner
from codeprobe.sandbox.agent_container import containerize_argv
from codeprobe.sandbox.image_config import CONTAINER_CONFIG_ENV
from codeprobe.snapshot.evidence_validation import ARTIFACT_FILENAMES
from codeprobe.snapshot.redact import PUBLISHABLE_DEFAULT, SIGNING_KEY_ENV
from codeprobe.trace.content_policy import REDACTED_AUTH, REDACTED_ENV

REPO_ROOT = Path(__file__).resolve().parents[2]
INVENTORY_PATH = REPO_ROOT / "docs" / "security" / "enterprise_inventory.json"
_SENSITIVE_ENV_MARKERS = (
    "KEY",
    "TOKEN",
    "AUTH",
    "PASSWORD",
    "SECRET",
    "CREDENTIAL",
    "PROXY",
    "BASE_URL",
    "CONFIG_DIR",
    "SSL_CERT",
    "CA_BUNDLE",
)
_LOWER_PROXY_ENV_VARS = {"http_proxy", "https_proxy", "no_proxy", "all_proxy"}
_NON_ENV_SYMBOLS = {
    "AUTH",
    "DIGEST",
    "DIRTY_CHECKOUT",
    "EXPERIMENT_DIR",
    "OFFLINE_NET_ATTEMPT",
    "PURGE_REFUSED",
    "SNAPSHOT_DIR",
    "UNCONTAINED_REFUSED",
}
_PROVIDER_NEUTRAL_FORBIDDEN_TERMS = (
    "sourcegraph",
    "field_engineering",
    "solutions_engineering",
    "cp-zca-pilot",
    "codeprobe-2z76",
    "participant_",
    "other_sourcegraph_personnel",
)
_ENV_TOKEN_PREFIXES = (
    "ALL_",
    "ANTHROPIC_",
    "AWS_",
    "AZURE_",
    "CLAUDE_",
    "CODEPROBE_",
    "COPILOT_",
    "CURL_",
    "DBUS_",
    "GITHUB_",
    "GOOGLE_",
    "HTTP_",
    "HTTPS_",
    "LC_",
    "NODE_",
    "NO_",
    "NPM_",
    "OPENAI_",
    "REQUESTS_",
    "SSL_",
    "XDG_",
)
_BARE_ENV_NAMES = {
    "CARGO_HOME",
    "GOPATH",
    "GOROOT",
    "HOME",
    "LANG",
    "LOGNAME",
    "PATH",
    "PYTHONPATH",
    "RUSTUP_HOME",
    "TERM",
    "TMPDIR",
    "USER",
    "VIRTUAL_ENV",
}
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_SHELL_FENCE_RE = re.compile(r"```(?:bash|sh|shell)\n(.*?)```", re.DOTALL)
_IMAGE_REF_RE = re.compile(r"(?<![\w/.-])(?:[\w.-]+/)?[\w.-]+:[\w.-]+")


def _inventory() -> dict:
    return json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))


def _doc_corpus(inventory: dict) -> str:
    documents = inventory["documents"]
    return "\n".join(
        (REPO_ROOT / relpath).read_text(encoding="utf-8")
        for relpath in documents
    )


def _normalized(text: str) -> str:
    return " ".join(text.casefold().split())


def _readme_security_sections(readme: str) -> str:
    start = readme.index("\n## Security and enterprise deployment")
    end = readme.index("\n## License", start)
    return readme[start:end]


def _drift_checked_corpus(inventory: dict) -> str:
    parts: list[str] = []
    for relpath in inventory["drift_checked_documents"]:
        text = (REPO_ROOT / relpath).read_text(encoding="utf-8")
        if relpath == "README.md":
            text = _readme_security_sections(text)
        parts.append(text)
    return "\n".join(parts)


def _inline_code_spans(text: str) -> set[str]:
    return {match.group(1).strip() for match in _INLINE_CODE_RE.finditer(text)}


def _documented_commands(text: str) -> set[str]:
    commands: set[str] = set()
    for block in _SHELL_FENCE_RE.findall(text):
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("$ "):
                line = line[2:]
            if " # " in line:
                line = line.split(" # ", 1)[0].rstrip()
            if line.startswith(("codeprobe ", "docker build ")):
                if line.endswith("\\"):
                    commands.add(" ".join(line.removesuffix("\\").split()[:2]))
                else:
                    commands.add(line)
    return commands


def _documented_repository_paths(text: str) -> set[str]:
    prefixes = ("docs/", "scripts/", "src/", "tests/")
    file_names = {"AGENTS.md", "README.md", "SECURITY.md", "pyproject.toml"}
    return {
        span
        for span in _inline_code_spans(text)
        if span.startswith(prefixes) or span in file_names
    }


def _documented_local_artifact_paths(text: str) -> set[str]:
    names = {
        span
        for span in _inline_code_spans(text)
        if span.startswith(".codeprobe/")
        or span
        in {
            ".codeprobe/",
            "SNAPSHOT_DIR",
            "approved-evidence",
            "codeprobe-mcp-*.json",
            "trace.db",
        }
        or span.endswith(("/trace.db", "/agent_output.txt", "/agent_error.txt"))
    }
    return names


def _deletion_table_artifacts(text: str) -> set[str]:
    table = text.split("Deletion responsibilities:\n\n", 1)[1].split("\n\n", 1)[0]
    rows = table.splitlines()[2:]
    artifacts: set[str] = set()
    for row in rows:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        if len(cells) >= 3:
            artifacts.add(cells[0])
    return artifacts


def _is_documented_env_token(token: str) -> bool:
    if token in _NON_ENV_SYMBOLS:
        return False
    if token in _LOWER_PROXY_ENV_VARS:
        return True
    if token.upper() != token:
        return False
    return (
        token.startswith(_ENV_TOKEN_PREFIXES)
        or token in _BARE_ENV_NAMES
        or any(marker in token for marker in _SENSITIVE_ENV_MARKERS)
    )


def _documented_env_vars(text: str) -> set[str]:
    names: set[str] = set()
    for span in _inline_code_spans(text):
        for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", span):
            if _is_documented_env_token(token):
                names.add(token)
    return names


def _documented_image_refs(text: str) -> set[str]:
    return {
        match.group(0)
        for match in _IMAGE_REF_RE.finditer(text)
        if not match.group(0).startswith("sha256:")
    }


def _dockerfile_text(relpath: str) -> str:
    return (REPO_ROOT / relpath).read_text(encoding="utf-8")


def _dockerfile_from_image(dockerfile: str) -> str:
    for line in dockerfile.splitlines():
        if line.startswith("FROM "):
            return line.split()[1]
    raise AssertionError("Dockerfile has no FROM instruction")


def _dockerfile_sets_user(dockerfile: str) -> bool:
    return any(line.startswith("USER ") for line in dockerfile.splitlines())


def _help_for(command: list[str]) -> str:
    result = CliRunner().invoke(main, [*command, "--help"])
    assert result.exit_code == 0, result.output
    return result.output


def test_security_entry_points_exist_and_are_linked_from_readme() -> None:
    inventory = _inventory()
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    for relpath in inventory["documents"]:
        path = REPO_ROOT / relpath
        assert path.is_file(), f"{relpath} is missing"

    assert "SECURITY.md" in readme
    assert "docs/security/enterprise_deployment.md" in readme


def test_vulnerability_reporting_channel_is_actionable() -> None:
    security = (REPO_ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert "https://github.com/sjarmak/codeprobe/security/advisories/new" in security
    assert "do not include exploit details" in security.casefold()


def test_documented_repository_paths_resolve() -> None:
    inventory = _inventory()
    corpus = _doc_corpus(inventory)

    for relpath in inventory["repository_paths"]:
        assert relpath in corpus, f"{relpath} is not documented"
        assert (REPO_ROOT / relpath).exists(), f"{relpath} does not resolve"


def test_documented_repository_path_references_are_inventoried() -> None:
    inventory = _inventory()
    documented = _documented_repository_paths(_drift_checked_corpus(inventory))
    inventoried = set(inventory["repository_paths"])

    assert documented <= inventoried, (
        "Documented repository paths missing from enterprise inventory: "
        + ", ".join(sorted(documented - inventoried))
    )


def test_documented_local_artifact_paths_are_inventoried() -> None:
    inventory = _inventory()
    drift_corpus = _drift_checked_corpus(inventory)
    doc_corpus = _doc_corpus(inventory)
    documented = _documented_local_artifact_paths(drift_corpus)
    inventoried = {entry["path"] for entry in inventory["local_artifact_paths"]}

    assert documented <= inventoried, (
        "Documented local artifact paths missing from enterprise inventory: "
        + ", ".join(sorted(documented - inventoried))
    )
    for entry in inventory["local_artifact_paths"]:
        assert entry["path"] in doc_corpus, f"{entry['path']} is not documented"


def test_deletion_responsibility_rows_are_inventoried() -> None:
    inventory = _inventory()
    guide = (REPO_ROOT / "docs/security/enterprise_deployment.md").read_text(
        encoding="utf-8"
    )
    documented = _deletion_table_artifacts(guide)
    inventoried = {entry["artifact"] for entry in inventory["deletion_responsibilities"]}

    assert documented == inventoried


def test_documented_container_images_match_code_constants() -> None:
    inventory = _inventory()
    corpus = _doc_corpus(inventory)

    for image in inventory["images"]:
        assert image["name"] in corpus
        assert image["name"] == getattr(sandbox_runner, image["constant"])
        assert (REPO_ROOT / image["dockerfile"]).is_file()
        assert image["base_image"] in corpus
        assert _dockerfile_from_image(_dockerfile_text(image["dockerfile"])) == image["base_image"]


def test_documented_image_references_are_inventoried() -> None:
    inventory = _inventory()
    documented = _documented_image_refs(_drift_checked_corpus(inventory))
    inventoried = {image["name"] for image in inventory["images"]} | {
        image["base_image"] for image in inventory["images"]
    } | {
        image["base_image_name"] for image in inventory["images"]
    }

    assert documented <= inventoried, (
        "Documented image references missing from enterprise inventory: "
        + ", ".join(sorted(documented - inventoried))
    )


def test_documented_container_supply_chain_contract_matches_dockerfiles() -> None:
    inventory = _inventory()
    corpus = _normalized(_doc_corpus(inventory))

    for image in inventory["images"]:
        dockerfile = _dockerfile_text(image["dockerfile"])
        assert "@sha256:" in _dockerfile_from_image(dockerfile)
        assert image["supply_chain_contract"]["base_image_pin"] == "sha256-digest"
        assert image["supply_chain_contract"]["runtime_user"] == "non-root"
        assert _dockerfile_sets_user(dockerfile)
        for phrase in image["supply_chain_contract"]["required_phrases"]:
            assert _normalized(phrase) in corpus, (
                f"{image['name']} supply-chain contract missing {phrase!r}"
            )

    agent = next(image for image in inventory["images"] if image["constant"] == "DEFAULT_AGENT_IMAGE")
    agent_dockerfile = _dockerfile_text(agent["dockerfile"])
    assert 'ARG CLAUDE_CODE_VERSION="' not in agent_dockerfile
    assert "ARG CLAUDE_CODE_VERSION=" in agent_dockerfile
    assert "ARG CLAUDE_CODE_INTEGRITY=" in agent_dockerfile
    assert '"@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"' in agent_dockerfile


def test_documented_commands_match_cli_and_paths() -> None:
    inventory = _inventory()
    corpus = _doc_corpus(inventory)

    for command in inventory["commands"]:
        text = command["command"]
        assert text in corpus, f"{text!r} is not documented"
        if text.startswith("docker build"):
            parts = text.split()
            dockerfile = parts[parts.index("-f") + 1]
            assert (REPO_ROOT / dockerfile).is_file()
            if "image_constant" in command:
                expected_image = getattr(sandbox_runner, command["image_constant"])
                assert parts[parts.index("-t") + 1] == expected_image
        if "help" in command:
            help_text = _help_for(command["help"])
            for token in command.get("required_help_tokens", ()):
                assert token in help_text, f"{token} missing from {' '.join(command['help'])} help"


def test_documented_command_references_are_inventoried() -> None:
    inventory = _inventory()
    documented = _documented_commands(_drift_checked_corpus(inventory))
    inventoried = {command["command"] for command in inventory["commands"]}

    assert documented <= inventoried, (
        "Documented commands missing from enterprise inventory: "
        + ", ".join(sorted(documented - inventoried))
    )


def _credential_ttl_mentions(name: str) -> bool:
    return name in inspect.getsource(credential_ttl)


_ENV_CHECKS: dict[str, Callable[[str], bool]] = {
    "adapter_whitelist": lambda name: name in adapter_base._ADAPTER_ENV_WHITELIST,
    "container_ca_mount": lambda name: name
    in adapter_base._PRIVATE_CA_FILE_ENV_KEYS + adapter_base._PRIVATE_CA_DIR_ENV_KEYS,
    "container_passthrough": lambda name: name in adapter_base._CONTAINER_ENV_KEYS,
    "container_excluded": lambda name: name in adapter_base._CONTAINER_ENV_EXCLUDED,
    "credential_ttl": _credential_ttl_mentions,
    "container_config": lambda name: name == CONTAINER_CONFIG_ENV,
    "offline_helper": lambda name: name == "CODEPROBE_OFFLINE" and name in inspect.getsource(guard_offline),
    "run_sets_offline": lambda name: name == "CODEPROBE_OFFLINE" and name in inspect.getsource(run_cmd.run_eval),
    "sandbox_helper": lambda name: name == "CODEPROBE_SANDBOX" and name in inspect.getsource(core_sandbox),
    "snapshot_signing_key": lambda name: name == SIGNING_KEY_ENV,
}


def test_documented_environment_variables_match_implementation() -> None:
    inventory = _inventory()
    corpus = _doc_corpus(inventory)

    for env_var in inventory["environment_variables"]:
        name = env_var["name"]
        assert name in corpus, f"{name} is not documented"
        for check in env_var["checks"]:
            assert _ENV_CHECKS[check](name), f"{name} failed {check}"


def test_adapter_environment_inventory_is_complete() -> None:
    inventory = _inventory()
    env_by_check = {
        check: {
            entry["name"]
            for entry in inventory["environment_variables"]
            if check in entry["checks"]
        }
        for check in ("adapter_whitelist", "container_passthrough", "container_excluded")
    }

    assert env_by_check["adapter_whitelist"] == adapter_base._ADAPTER_ENV_WHITELIST
    assert env_by_check["container_passthrough"] == adapter_base._CONTAINER_ENV_KEYS
    assert env_by_check["container_excluded"] == adapter_base._CONTAINER_ENV_EXCLUDED


def test_documented_environment_references_are_inventoried() -> None:
    inventory = _inventory()
    documented = _documented_env_vars(_drift_checked_corpus(inventory))
    inventoried = {entry["name"] for entry in inventory["environment_variables"]}

    assert documented <= inventoried, (
        "Documented environment variables missing from enterprise inventory: "
        + ", ".join(sorted(documented - inventoried))
    )


def test_forwarded_sensitive_environment_variables_are_inventoried() -> None:
    inventory = _inventory()
    documented = {entry["name"] for entry in inventory["environment_variables"]}
    forwarded = adapter_base._ADAPTER_ENV_WHITELIST | adapter_base._CONTAINER_ENV_KEYS
    sensitive = {
        name
        for name in forwarded
        if any(marker in name for marker in _SENSITIVE_ENV_MARKERS)
    }

    assert sensitive <= documented, (
        "Forwarded credential-like env vars missing from enterprise inventory: "
        + ", ".join(sorted(sensitive - documented))
    )


def _agent_container_argv_with_mounted_inputs() -> list[str]:
    return containerize_argv(
        ["claude", "-p", "prompt"],
        engine="docker",
        workspace=Path("/workspace"),
        config_dir=Path("/tmp/codeprobe-claude/slot-0"),
        mcp_tmpfile="/tmp/codeprobe-mcp-abcd.json",
        env_keys=["ANTHROPIC_API_KEY"],
        image=f"registry.example/codeprobe/{sandbox_runner.DEFAULT_AGENT_IMAGE}",
        name="codeprobe-agent-test",
        env={"ANTHROPIC_API_KEY": "sk-test"},
    )


def _agent_container_argv_without_optional_mounts() -> list[str]:
    return containerize_argv(
        ["claude", "-p", "prompt"],
        engine="docker",
        workspace=Path("/workspace"),
        config_dir=None,
        mcp_tmpfile=None,
        env_keys=[],
        image=f"registry.example/codeprobe/{sandbox_runner.DEFAULT_AGENT_IMAGE}",
        name="codeprobe-agent-test",
    )


def _agent_container_uses_bridge_network() -> bool:
    argv = _agent_container_argv_with_mounted_inputs()
    return "--network=bridge" in argv


def _scoring_container_argv() -> list[str]:
    return sandbox_runner._build_run_command(
        "docker",
        ["bash", "tests/test.sh"],
        {"/tmp/codeprobe-score-abc": "/tmp/codeprobe-score-abc"},
        allow_writes=True,
        image=f"registry.example/codeprobe/{sandbox_runner.DEFAULT_SCORING_IMAGE}",
        workdir="/tmp/codeprobe-score-abc/task",
        env={"AGENT_OUTPUT": "/tmp/codeprobe-score-abc/agent_output.txt"},
    )


def _scoring_container_defaults_to_no_network() -> bool:
    argv = _scoring_container_argv()
    return "--network=none" in argv


def _agent_container_mounts_expected_paths() -> bool:
    argv = _agent_container_argv_with_mounted_inputs()
    mounts = {
        argv[index + 1]
        for index, token in enumerate(argv[:-1])
        if token == "-v"
    }
    env_args = {
        argv[index + 1]
        for index, token in enumerate(argv[:-1])
        if token == "-e"
    }
    return mounts == {
        "/workspace:/workspace:rw",
        "/tmp/codeprobe-claude/slot-0:/tmp/codeprobe-claude/slot-0:rw",
        "/tmp/codeprobe-mcp-abcd.json:/tmp/codeprobe-mcp-abcd.json:ro",
    } and {"HOME=/tmp", "TMPDIR=/tmp", "ANTHROPIC_API_KEY"} <= env_args


def _agent_container_mounts_private_ca_paths() -> bool:
    with tempfile.TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        ca_file = root / "corp.pem"
        ca_file.write_text("certificate", encoding="utf-8")
        ca_dir = root / "certs"
        ca_dir.mkdir()
        mounts, env_values = adapter_base._container_private_ca(
            {
                "SSL_CERT_FILE": str(ca_file),
                "REQUESTS_CA_BUNDLE": str(ca_file),
                "SSL_CERT_DIR": str(ca_dir),
            }
        )
        file_target = Path("/etc/codeprobe/ca/00-corp.pem")
        dir_target = Path("/etc/codeprobe/ca/01-certs")
        invalid = adapter_base._container_private_ca(
            {"SSL_CERT_FILE": str(root / "missing.pem")}
        )
        return mounts == [
            (ca_file.resolve(), file_target),
            (ca_dir.resolve(), dir_target),
        ] and env_values == {
            "SSL_CERT_FILE": str(file_target),
            "REQUESTS_CA_BUNDLE": str(file_target),
            "SSL_CERT_DIR": str(dir_target),
        } and invalid == ([], {})


def _agent_container_uses_valueless_secret_args() -> bool:
    argv = _agent_container_argv_with_mounted_inputs()
    return "ANTHROPIC_API_KEY" in argv and "sk-test" not in argv


def _claude_session_mirrors_live_config_with_symlinks() -> bool:
    source = inspect.getsource(claude_adapter._build_mirror_slot_env)
    return "symlink_to" in source and "credentials file" in source


def _agent_container_has_runtime_hardening() -> bool:
    argv = _agent_container_argv_without_optional_mounts()
    return {
        "--pull=never",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--cpus=2",
        "--memory=4g",
        "--memory-swap=4g",
        "--pids-limit=256",
        "--read-only",
    } <= set(argv) and "/tmp:rw,nosuid,nodev,size=128m,mode=1777" in argv


def _scoring_container_has_runtime_hardening() -> bool:
    argv = _scoring_container_argv()
    return {
        "--pull=never",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--cpus=2",
        "--memory=4g",
        "--memory-swap=4g",
        "--pids-limit=256",
        "--read-only",
    } <= set(argv) and "/tmp:rw,nosuid,nodev,size=128m,mode=1777" in argv


def _purge_targets_only_scoped_artifacts() -> bool:
    callback = purge_cmd.purge.callback
    if callback is None:
        return False
    source = inspect.getsource(callback)
    return (
        'root / ".codeprobe"' in source
        and "_experiment_dirs(codeprobe_dir)" in source
        and "_MCP_TEMPFILE_PATTERN" in source
        and "_escaping_path(cand.path, boundary)" in source
    )


def _evidence_bundle_uses_fixed_allowlist() -> bool:
    return ARTIFACT_FILENAMES == (
        "run-manifest.json",
        "sample-attestation.json",
        "aggregate-results.json",
        "findings.md",
        "support-log.json",
    )


_SECURITY_CLAIMS: dict[str, Callable[[], bool]] = {
    "agent_container_network_bridge": _agent_container_uses_bridge_network,
    "agent_container_mounts_expected_paths": _agent_container_mounts_expected_paths,
    "agent_container_mounts_private_ca_paths": _agent_container_mounts_private_ca_paths,
    "agent_container_valueless_secret_args": _agent_container_uses_valueless_secret_args,
    "claude_session_live_symlinks": _claude_session_mirrors_live_config_with_symlinks,
    "agent_container_runtime_hardening": _agent_container_has_runtime_hardening,
    "scoring_container_network_none": _scoring_container_defaults_to_no_network,
    "scoring_container_runtime_hardening": _scoring_container_has_runtime_hardening,
    "purge_cleartext_disclosure": lambda: "cleartext" in _DISCLOSURE.lower(),
    "purge_tempfile_pattern": lambda: _MCP_TEMPFILE_PATTERN == "codeprobe-mcp-*.json",
    "purge_scoped_artifacts": _purge_targets_only_scoped_artifacts,
    "evidence_bundle_fixed_allowlist": _evidence_bundle_uses_fixed_allowlist,
    "snapshot_default_hashes_only": lambda: PUBLISHABLE_DEFAULT == "hashes-only",
    "trace_redaction_records_env_and_auth": lambda: REDACTED_ENV == "[REDACTED-ENV]"
    and REDACTED_AUTH == "[REDACTED-AUTH]",
    "offline_guard_is_env_gated": lambda: "CODEPROBE_OFFLINE" in inspect.getsource(guard_offline),
    "host_fallback_requires_uncontained": lambda: "--uncontained" in inspect.getsource(
        scoring_sandbox._missing_image_refusal
    ),
}


def test_documented_security_claims_match_implementation() -> None:
    inventory = _inventory()
    corpus = _normalized(_doc_corpus(inventory))

    for claim in inventory["security_claims"]:
        for phrase in claim["required_phrases"]:
            assert _normalized(phrase) in corpus, f"{claim['id']} missing phrase {phrase!r}"
        for check in claim["checks"]:
            assert _SECURITY_CLAIMS[check](), f"{claim['id']} failed {check}"


def test_new_security_docs_are_provider_neutral() -> None:
    inventory = _inventory()

    for relpath in inventory["provider_neutral_documents"]:
        content = (REPO_ROOT / relpath).read_text(encoding="utf-8").casefold()
        assert all(term.casefold() not in content for term in _PROVIDER_NEUTRAL_FORBIDDEN_TERMS), (
            f"{relpath} contains provider-specific or engagement-specific language"
        )
