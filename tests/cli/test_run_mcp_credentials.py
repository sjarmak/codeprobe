"""CLI preflight must reject unusable MCP credentials before dispatch."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli import main


def test_experiment_config_help_requires_exported_secret_references() -> None:
    result = CliRunner().invoke(
        main,
        ["experiment", "add-config", "--help"],
    )

    assert result.exit_code == 0
    assert "${EXPORTED_VAR}" in result.output


def _experiment(tmp_path: Path, authorization: str) -> Path:
    exp_dir = tmp_path / ".codeprobe" / "credential-preflight"
    exp_dir.mkdir(parents=True)
    payload = {
        "name": "credential-preflight",
        "tasks_dir": "tasks",
        "configs": [
            {
                "label": "mcp",
                "agent": "claude",
                "mcp_config": {
                    "mcpServers": {
                        "sourcegraph": {
                            "type": "http",
                            "url": "https://sourcegraph.example/.api/mcp/all",
                            "headers": {"Authorization": authorization},
                        }
                    }
                },
            }
        ],
    }
    (exp_dir / "experiment.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    return exp_dir


@pytest.mark.parametrize(
    ("authorization", "expected"),
    [
        ("[REDACTED]", "redacted"),
        ("token ${MISSING_SOURCEGRAPH_TOKEN}", "MISSING_SOURCEGRAPH_TOKEN"),
    ],
)
def test_run_rejects_unusable_mcp_credential_before_adapter_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authorization: str,
    expected: str,
) -> None:
    exp_dir = _experiment(tmp_path, authorization)
    monkeypatch.delenv("MISSING_SOURCEGRAPH_TOKEN", raising=False)

    def _unexpected_resolve(_name: str):
        pytest.fail("adapter resolution must not run before credential preflight")

    monkeypatch.setattr("codeprobe.cli.run_cmd.resolve", _unexpected_resolve)

    result = CliRunner().invoke(
        main,
        ["run", str(exp_dir), "--dry-run", "--force-plain"],
    )

    assert result.exit_code != 0
    assert "UNUSABLE_MCP_CREDENTIAL" in result.output
    assert expected in result.output
