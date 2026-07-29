"""Machine-check the enterprise security documentation inventory."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from pathlib import Path

from click.testing import CliRunner

import codeprobe.cli.run_cmd as run_cmd
import codeprobe.net.credential_ttl as credential_ttl
from codeprobe.adapters import _base as adapter_base
from codeprobe.cli import main
from codeprobe.cli.purge_cmd import _DISCLOSURE, _MCP_TEMPFILE_PATTERN
from codeprobe.core import sandbox as core_sandbox
from codeprobe.core.scoring import sandbox as scoring_sandbox
from codeprobe.net.offline import guard_offline
from codeprobe.sandbox import runner as sandbox_runner
from codeprobe.sandbox.agent_container import containerize_argv
from codeprobe.snapshot.redact import PUBLISHABLE_DEFAULT, SIGNING_KEY_ENV
from codeprobe.trace.content_policy import REDACTED_AUTH, REDACTED_ENV

REPO_ROOT = Path(__file__).resolve().parents[2]
INVENTORY_PATH = REPO_ROOT / "docs" / "security" / "enterprise_inventory.json"
_SENSITIVE_ENV_MARKERS = ("KEY", "TOKEN", "AUTH")


def _inventory() -> dict:
    return json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))


def _doc_corpus(inventory: dict) -> str:
    documents = inventory["documents"]
    return "\n".join(
        (REPO_ROOT / relpath).read_text(encoding="utf-8")
        for relpath in documents
    )


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


def test_documented_repository_paths_resolve() -> None:
    inventory = _inventory()
    corpus = _doc_corpus(inventory)

    for relpath in inventory["repository_paths"]:
        assert relpath in corpus, f"{relpath} is not documented"
        assert (REPO_ROOT / relpath).exists(), f"{relpath} does not resolve"


def test_documented_container_images_match_code_constants() -> None:
    inventory = _inventory()
    corpus = _doc_corpus(inventory)

    for image in inventory["images"]:
        assert image["name"] in corpus
        assert image["name"] == getattr(sandbox_runner, image["constant"])
        assert (REPO_ROOT / image["dockerfile"]).is_file()


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


def _credential_ttl_mentions(name: str) -> bool:
    return name in inspect.getsource(credential_ttl)


_ENV_CHECKS: dict[str, Callable[[str], bool]] = {
    "adapter_whitelist": lambda name: name in adapter_base._ADAPTER_ENV_WHITELIST,
    "container_passthrough": lambda name: name in adapter_base._CONTAINER_ENV_KEYS,
    "container_excluded": lambda name: name in adapter_base._CONTAINER_ENV_EXCLUDED,
    "credential_ttl": _credential_ttl_mentions,
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


def _agent_container_uses_bridge_network() -> bool:
    argv = containerize_argv(
        ["claude", "-p", "prompt"],
        engine="docker",
        workspace=Path("/workspace"),
        config_dir=Path("/tmp/codeprobe-claude/slot-0"),
        mcp_tmpfile="/tmp/codeprobe-mcp-abcd.json",
        env_keys=["ANTHROPIC_API_KEY"],
        image=sandbox_runner.DEFAULT_AGENT_IMAGE,
        name="codeprobe-agent-test",
        env={"ANTHROPIC_API_KEY": "sk-test"},
    )
    return "--network=bridge" in argv


def _scoring_container_defaults_to_no_network() -> bool:
    argv = sandbox_runner._build_run_command(
        "docker",
        ["bash", "tests/test.sh"],
        {"/tmp/codeprobe-score-abc": "/tmp/codeprobe-score-abc"},
        allow_writes=True,
        image=sandbox_runner.DEFAULT_SCORING_IMAGE,
        workdir="/tmp/codeprobe-score-abc/task",
        env={"AGENT_OUTPUT": "/tmp/codeprobe-score-abc/agent_output.txt"},
    )
    return "--network=none" in argv


_SECURITY_CLAIMS: dict[str, Callable[[], bool]] = {
    "agent_container_network_bridge": _agent_container_uses_bridge_network,
    "scoring_container_network_none": _scoring_container_defaults_to_no_network,
    "purge_cleartext_disclosure": lambda: "cleartext" in _DISCLOSURE.lower(),
    "purge_tempfile_pattern": lambda: _MCP_TEMPFILE_PATTERN == "codeprobe-mcp-*.json",
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
    corpus = _doc_corpus(inventory).casefold()

    for claim in inventory["security_claims"]:
        for phrase in claim["required_phrases"]:
            assert phrase.casefold() in corpus, f"{claim['id']} missing phrase {phrase!r}"
        for check in claim["checks"]:
            assert _SECURITY_CLAIMS[check](), f"{claim['id']} failed {check}"


def test_new_security_docs_are_provider_neutral() -> None:
    inventory = _inventory()
    forbidden = inventory["provider_neutral_forbidden_terms"]

    for relpath in inventory["provider_neutral_documents"]:
        content = (REPO_ROOT / relpath).read_text(encoding="utf-8").casefold()
        assert all(term.casefold() not in content for term in forbidden), (
            f"{relpath} contains provider-specific or engagement-specific language"
        )
