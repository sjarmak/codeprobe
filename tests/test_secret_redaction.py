"""Tests for secret redaction in config/experiment serialization.

Verifies that Authorization headers and token values are never
exposed in ExperimentConfig repr, experiment.json on disk, or
any serialization path that could end up in logs.
"""

from __future__ import annotations

import json
from pathlib import Path

from codeprobe.models.experiment import ExperimentConfig

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_MCP_CONFIG_WITH_TOKEN = {
    "mcpServers": {
        "sourcegraph": {
            "type": "http",
            "url": "https://sourcegraph.com/.api/mcp/v1",
            "headers": {
                "Authorization": "token sgp_abcdef1234567890abcdef1234567890",
            },
        }
    }
}

_REAL_TOKEN = "sgp_abcdef1234567890abcdef1234567890"


def _config_with_token() -> ExperimentConfig:
    return ExperimentConfig(
        label="with-mcp",
        agent="claude",
        model="claude-sonnet-4-6",
        mcp_config=_MCP_CONFIG_WITH_TOKEN,
    )


# ---------------------------------------------------------------------------
# repr / str never leaks tokens
# ---------------------------------------------------------------------------


class TestExperimentConfigRepr:
    """ExperimentConfig.__repr__ must redact Authorization header values."""

    def test_repr_does_not_contain_token(self) -> None:
        config = _config_with_token()
        representation = repr(config)
        assert _REAL_TOKEN not in representation

    def test_repr_shows_redacted_marker(self) -> None:
        config = _config_with_token()
        representation = repr(config)
        assert "[REDACTED]" in representation

    def test_repr_preserves_non_sensitive_fields(self) -> None:
        config = _config_with_token()
        representation = repr(config)
        assert "with-mcp" in representation
        assert "claude-sonnet-4-6" in representation

    def test_repr_without_mcp_config_is_clean(self) -> None:
        config = ExperimentConfig(label="baseline")
        representation = repr(config)
        assert "baseline" in representation
        assert "[REDACTED]" not in representation

    def test_str_does_not_contain_token(self) -> None:
        config = _config_with_token()
        assert _REAL_TOKEN not in str(config)


# ---------------------------------------------------------------------------
# redact_mcp_headers utility
# ---------------------------------------------------------------------------


class TestRedactMcpHeaders:
    """redact_mcp_headers returns a new dict with Authorization values masked."""

    def test_redacts_authorization_header(self) -> None:
        from codeprobe.config.redact import redact_mcp_headers

        result = redact_mcp_headers(_MCP_CONFIG_WITH_TOKEN)
        auth = result["mcpServers"]["sourcegraph"]["headers"]["Authorization"]
        assert _REAL_TOKEN not in auth
        assert "[REDACTED]" in auth

    def test_preserves_non_sensitive_keys(self) -> None:
        from codeprobe.config.redact import redact_mcp_headers

        result = redact_mcp_headers(_MCP_CONFIG_WITH_TOKEN)
        assert result["mcpServers"]["sourcegraph"]["type"] == "http"
        assert (
            result["mcpServers"]["sourcegraph"]["url"]
            == "https://sourcegraph.com/.api/mcp/v1"
        )

    def test_returns_new_dict_no_mutation(self) -> None:
        from codeprobe.config.redact import redact_mcp_headers

        original_auth = _MCP_CONFIG_WITH_TOKEN["mcpServers"]["sourcegraph"]["headers"][
            "Authorization"
        ]
        redact_mcp_headers(_MCP_CONFIG_WITH_TOKEN)
        assert (
            _MCP_CONFIG_WITH_TOKEN["mcpServers"]["sourcegraph"]["headers"][
                "Authorization"
            ]
            == original_auth
        )

    def test_none_input_returns_none(self) -> None:
        from codeprobe.config.redact import redact_mcp_headers

        assert redact_mcp_headers(None) is None

    def test_empty_dict_returns_empty(self) -> None:
        from codeprobe.config.redact import redact_mcp_headers

        assert redact_mcp_headers({}) == {}

    def test_redacts_bearer_tokens(self) -> None:
        from codeprobe.config.redact import redact_mcp_headers

        config = {
            "mcpServers": {
                "api": {
                    "headers": {
                        "Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.long.token",
                    }
                }
            }
        }
        result = redact_mcp_headers(config)
        assert "eyJhbG" not in json.dumps(result)

    def test_redacts_multiple_servers(self) -> None:
        from codeprobe.config.redact import redact_mcp_headers

        config = {
            "mcpServers": {
                "sg": {"headers": {"Authorization": "token secret1_long_enough"}},
                "other": {"headers": {"Authorization": "Bearer secret2_long_enough"}},
            }
        }
        result = redact_mcp_headers(config)
        dumped = json.dumps(result)
        assert "secret1" not in dumped
        assert "secret2" not in dumped

    def test_redacts_bare_token_without_scheme_prefix(self) -> None:
        """Authorization values without a scheme prefix are still redacted."""
        from codeprobe.config.redact import redact_mcp_headers

        config = {
            "mcpServers": {
                "sg": {"headers": {"Authorization": "sgp_raw_token_no_prefix"}}
            }
        }
        result = redact_mcp_headers(config)
        assert "sgp_raw_token_no_prefix" not in json.dumps(result)
        assert result["mcpServers"]["sg"]["headers"]["Authorization"] == "[REDACTED]"

    def test_handles_nested_non_standard_structure(self) -> None:
        """Config without mcpServers key passes through unchanged."""
        from codeprobe.config.redact import redact_mcp_headers

        config = {"type": "http", "url": "https://example.com"}
        result = redact_mcp_headers(config)
        assert result == config

    # --- CLI-arg token redaction (BUG-10: mcp-remote --header pattern) ---

    def test_redacts_token_in_args_header_flag(self) -> None:
        """Tokens passed as --header args to mcp-remote must be redacted."""
        from codeprobe.config.redact import redact_mcp_headers

        config = {
            "mcpServers": {
                "sourcegraph": {
                    "type": "stdio",
                    "command": "npx",
                    "args": [
                        "-y",
                        "mcp-remote",
                        "https://demo.sourcegraph.com/.api/mcp/all",
                        "--header",
                        "Authorization: token sgp_db0c9af43ab9c365_fake_test_value",
                    ],
                    "env": {},
                }
            }
        }
        result = redact_mcp_headers(config)
        dumped = json.dumps(result)
        assert "sgp_db0c9af43ab9c365" not in dumped
        assert "[REDACTED]" in dumped

    def test_redacts_bearer_token_in_args(self) -> None:
        """Bearer tokens in CLI args are also redacted."""
        from codeprobe.config.redact import redact_mcp_headers

        config = {
            "mcpServers": {
                "api": {
                    "command": "mcp-remote",
                    "args": [
                        "https://api.example.com",
                        "--header",
                        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
                    ],
                }
            }
        }
        result = redact_mcp_headers(config)
        assert "eyJhbG" not in json.dumps(result)

    def test_redacts_known_token_patterns_in_env(self) -> None:
        """Token-shaped values in env dict are redacted."""
        from codeprobe.config.redact import redact_mcp_headers

        config = {
            "mcpServers": {
                "sg": {
                    "command": "node",
                    "args": ["server.js"],
                    "env": {
                        "SRC_ACCESS_TOKEN": "sgp_abc123_def456789012345678901234567890",
                        "GITHUB_TOKEN": "ghp_1234567890abcdef1234567890abcdef12345678",
                        "OPENAI_API_KEY": "sk-proj-abc123def456ghi789",
                        "SAFE_VALUE": "not-a-secret",
                    },
                }
            }
        }
        result = redact_mcp_headers(config)
        env = result["mcpServers"]["sg"]["env"]
        assert "sgp_abc123" not in json.dumps(env)
        assert "ghp_1234567890" not in json.dumps(env)
        assert "sk-proj-abc123" not in json.dumps(env)
        assert env["SAFE_VALUE"] == "not-a-secret"

    def test_redacts_token_in_args_preserves_non_sensitive_args(self) -> None:
        """Non-sensitive args like URLs and flags should be unchanged."""
        from codeprobe.config.redact import redact_mcp_headers

        config = {
            "mcpServers": {
                "sg": {
                    "command": "npx",
                    "args": [
                        "-y",
                        "mcp-remote",
                        "https://example.com/.api/mcp/all",
                        "--header",
                        "Authorization: token sgp_secret_value_1234567890123456789012345",
                    ],
                }
            }
        }
        result = redact_mcp_headers(config)
        args = result["mcpServers"]["sg"]["args"]
        assert args[0] == "-y"
        assert args[1] == "mcp-remote"
        assert args[2] == "https://example.com/.api/mcp/all"
        assert args[3] == "--header"

    def test_no_mutation_of_original_with_args_tokens(self) -> None:
        """Original config must not be mutated when redacting args tokens."""
        from codeprobe.config.redact import redact_mcp_headers

        config = {
            "mcpServers": {
                "sg": {
                    "args": [
                        "--header",
                        "Authorization: token sgp_original_should_not_change_here",
                    ],
                }
            }
        }
        original_arg = config["mcpServers"]["sg"]["args"][1]
        redact_mcp_headers(config)
        assert config["mcpServers"]["sg"]["args"][1] == original_arg


# ---------------------------------------------------------------------------
# experiment.json serialization redacts tokens
# ---------------------------------------------------------------------------


class TestExperimentJsonRedaction:
    """save_experiment must redact tokens in mcp_config before writing to disk."""

    def test_experiment_json_does_not_contain_token(self, tmp_path: Path) -> None:
        from codeprobe.core.experiment import create_experiment_dir
        from codeprobe.models.experiment import Experiment

        exp = Experiment(
            name="redact-test",
            configs=[_config_with_token()],
        )
        exp_dir = create_experiment_dir(tmp_path, exp)

        raw = (exp_dir / "experiment.json").read_text()
        assert _REAL_TOKEN not in raw
        assert "[REDACTED]" in raw

    def test_experiment_json_round_trips_with_redacted_config(
        self, tmp_path: Path
    ) -> None:
        """Load after save still works, though token values are redacted."""
        from codeprobe.core.experiment import create_experiment_dir, load_experiment
        from codeprobe.models.experiment import Experiment

        exp = Experiment(
            name="round-trip",
            configs=[_config_with_token()],
        )
        exp_dir = create_experiment_dir(tmp_path, exp)

        loaded = load_experiment(exp_dir)
        assert loaded.name == "round-trip"
        assert loaded.configs[0].label == "with-mcp"
        # mcp_config should still be a dict (with redacted values)
        assert loaded.configs[0].mcp_config is not None

    def test_baseline_config_unaffected(self, tmp_path: Path) -> None:
        """Configs without mcp_config are unaffected by redaction."""
        from codeprobe.core.experiment import create_experiment_dir
        from codeprobe.models.experiment import Experiment

        exp = Experiment(
            name="baseline-only",
            configs=[ExperimentConfig(label="baseline")],
        )
        exp_dir = create_experiment_dir(tmp_path, exp)

        raw = (exp_dir / "experiment.json").read_text()
        assert "[REDACTED]" not in raw


class TestEnvVarReferencesPreserved:
    """Env-var templates like ``${VAR}`` must round-trip through redaction."""

    def test_header_env_ref_preserved(self) -> None:
        from codeprobe.config.redact import redact_mcp_headers

        cfg = {
            "mcpServers": {
                "sourcegraph": {
                    "headers": {"Authorization": "token ${SG_TOKEN}"},
                },
            },
        }
        result = redact_mcp_headers(cfg)
        assert (
            result["mcpServers"]["sourcegraph"]["headers"]["Authorization"]
            == "token ${SG_TOKEN}"
        )

    def test_literal_header_still_redacted(self) -> None:
        from codeprobe.config.redact import redact_mcp_headers

        cfg = {
            "mcpServers": {
                "sourcegraph": {
                    "headers": {"Authorization": "token sgp_abc123"},
                },
            },
        }
        result = redact_mcp_headers(cfg)
        assert (
            result["mcpServers"]["sourcegraph"]["headers"]["Authorization"]
            == "[REDACTED]"
        )

    def test_env_arg_preserved(self) -> None:
        from codeprobe.config.redact import redact_mcp_headers

        cfg = {
            "mcpServers": {
                "srv": {
                    "args": [
                        "--header",
                        "Authorization: token ${SG_TOKEN}",
                    ],
                },
            },
        }
        result = redact_mcp_headers(cfg)
        assert "${SG_TOKEN}" in result["mcpServers"]["srv"]["args"][1]

    def test_env_dict_ref_preserved(self) -> None:
        from codeprobe.config.redact import redact_mcp_headers

        cfg = {
            "mcpServers": {
                "srv": {
                    "env": {"SG_TOKEN_VAL": "${SG_TOKEN}"},
                },
            },
        }
        result = redact_mcp_headers(cfg)
        assert (
            result["mcpServers"]["srv"]["env"]["SG_TOKEN_VAL"] == "${SG_TOKEN}"
        )
