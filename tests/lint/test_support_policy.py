"""Mechanical consistency checks for the enterprise support contract."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "docs" / "support_policy.json"
SUPPORT_DOC = ROOT / "docs" / "support.md"


def _policy() -> dict[str, object]:
    raw = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def test_support_policy_is_versioned_and_matches_package_metadata() -> None:
    policy = _policy()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]

    assert policy["schema_version"] == 1
    assert policy["policy_version"] == "2026.1"
    assert policy["lifecycle"] == "beta"
    assert project["requires-python"] == ">=3.11,<3.14"
    assert "Development Status :: 4 - Beta" in project["classifiers"]
    assert "Operating System :: POSIX :: Linux" in project["classifiers"]
    assert "Development Status :: 3 - Alpha" not in project["classifiers"]


def test_policy_names_every_required_platform_and_status() -> None:
    policy = _policy()
    dimensions = policy["platforms"]
    assert isinstance(dimensions, dict)
    assert set(dimensions) == {
        "agent_cli",
        "architecture",
        "container_engine",
        "git",
        "operating_system",
        "python",
        "repository_language",
    }
    text = json.dumps(dimensions)
    assert all(status in text for status in ("supported", "preview", "unsupported"))
    assert all(version in text for version in ("3.11", "3.12", "3.13"))
    assert all(agent in text for agent in ("claude", "copilot", "codex"))
    assert all(language in text for language in ("python", "go", "javascript-typescript"))


def test_policy_declares_all_compatibility_windows_and_migrations() -> None:
    policy = _policy()
    schemas = policy["compatibility"]
    assert isinstance(schemas, dict)
    assert set(schemas) == {
        "cli",
        "configuration",
        "evidence",
        "result",
        "snapshot",
        "task",
    }
    for contract in schemas.values():
        assert isinstance(contract, dict)
        assert contract["read_window"]
        assert contract["write_version"]
        assert contract["unsupported_action"] in {"migrate", "refuse"}

    deprecation = policy["deprecation"]
    assert isinstance(deprecation, dict)
    assert deprecation["minimum_notice_minor_releases"] == 1
    assert deprecation["minimum_notice_days"] == 90


def test_readme_and_support_doc_agree_on_beta_boundary() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    support = SUPPORT_DOC.read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert "CodeProbe is beta software" in readme
    assert "CodeProbe is alpha software" not in readme
    assert "**Alpha.**" not in readme
    assert "[Enterprise support and compatibility](docs/support.md)" in readme
    assert "| Claude Code    | Supported" in readme
    assert "| GitHub Copilot | Preview" in readme
    assert "| Codex          | Unsupported" in readme
    assert "Runs Claude Code, Codex, or Copilot headless" not in readme
    assert "SNAPSHOT_UNSAFE_LEGACY_FORMAT" in changelog
    assert "docs/support.md" in changelog
    for heading in (
        "Support matrix",
        "Compatibility windows",
        "Deprecation and removal",
        "Upgrade procedure",
        "Source-free support bundle",
        "Support ownership",
    ):
        assert f"## {heading}" in support


def test_release_workflow_runs_exact_prior_wheel_upgrade_gate() -> None:
    workflow = yaml.load(
        (ROOT / ".github" / "workflows" / "publish.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    gate = workflow["jobs"]["gate"]
    text = str(gate)

    assert "upgrade_compatibility.py" in text
    assert "codeprobe-0.11.0-py3-none-any.whl" in text
    assert "a7797a1f4be4a6b4bd9ce73cb4ac868d8e26e2d4a23a3ecda040ee19105bfbf5" in text
    assert "--candidate-wheel candidate-dist/codeprobe-*.whl" in text
