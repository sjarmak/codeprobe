"""Task output scoring — run test.sh and return typed results.

Provides a Scorer protocol with three implementations:
- BinaryScorer: exit 0 = 1.0, else 0.0 (wraps legacy score_task_output)
- ContinuousScorer: reads float from reward.txt or stdout (0.0-1.0)
- CheckpointScorer: weighted checkpoint verifiers with partial credit

All scorers inherit the same sandbox security: temp dir isolation, filtered
environment, secret redaction, and configurable timeout.
"""

from __future__ import annotations

# ``shutil`` is re-exposed at package level because tests patch
# ``codeprobe.core.scoring.shutil.copytree`` (the pre-package module
# imported it at module scope).
import shutil  # noqa: F401

from codeprobe.core.scoring.ir import (  # noqa: F401
    _ZERO_SCORE,
    _compute_f1,
    _fbeta,
    _ir_metrics,
    _ir_reward_from_family,
    _lcs_length,
    _normalize_path,
    _normalize_symbol,
    score_count,
    score_dependency_chain,
    score_exact_match,
    score_file_list,
    score_symbol_list,
)
from codeprobe.core.scoring.materialize import (  # noqa: F401
    _GIT_TIMEOUT_SECONDS,
    _MAX_DIFF_BYTES,
    AgentState,
    _apply_diff,
    _capture_workspace_diff,
    _create_fresh_checkout,
    _is_git_repo,
    _materialize_workspace,
    _run_git,
)
from codeprobe.core.scoring.result import (  # noqa: F401
    ALLOWED_MATERIALIZATION,
    ALLOWED_VERDICTS,
    DEFAULT_FBETA_BETA,
    DEFAULT_IR_FAMILY,
    PASS_THRESHOLD,
    SCORER_FAMILIES,
    Scorer,
    ScoreResult,
    _read_fbeta_beta,
    _select_ir_family,
    read_task_metadata,
    read_task_verification,
    scorer_accepts_agent_state,
    scorer_accepts_low_confidence_threshold,
)
from codeprobe.core.scoring.sandbox import (  # noqa: F401
    _SAFE_ENV_KEYS,
    _TOKEN_PATTERN,
    COPYTREE_IGNORE,
    SCORE_TIMEOUT_SECONDS,
    _run_in_sandbox,
    _safe_env,
    _SandboxRun,
    _thread_env_overrides,
    sanitize_secrets,
    scorer_env_override,
)
from codeprobe.core.scoring.scorers import (  # noqa: F401
    _IR_LIST_ANSWER_TYPES,
    _MAX_GROUND_TRUTH_BYTES,
    _ORACLE_TYPE_SCORERS,
    _WEIGHT_TOLERANCE,
    VALID_REWARD_TYPES,
    ArtifactScorer,
    BinaryScorer,
    CheckpointScorer,
    ContinuousScorer,
    DualScorer,
    OracleChecksScorer,
    _cli_main,
    _find_answer_file,
    _load_json_file,
    _parse_float_score,
    _safe_leg_score,
    get_scorer,
    score_task_output,
    validate_ground_truth,
)

# Explicit re-export surface. This package deliberately re-exposes both the
# public scorer API and a number of internal helpers/constants (including
# underscore-prefixed ones) that sibling packages import from
# ``codeprobe.core.scoring`` and that tests patch here. Under mypy ``strict``
# (``no_implicit_reexport``), a re-exported name must be listed in ``__all__``
# or aliased; ``__all__`` keeps the surface declared in one place.
__all__ = [
    # ir
    "_ZERO_SCORE",
    "_compute_f1",
    "_fbeta",
    "_ir_metrics",
    "_ir_reward_from_family",
    "_lcs_length",
    "_normalize_path",
    "_normalize_symbol",
    "score_count",
    "score_dependency_chain",
    "score_exact_match",
    "score_file_list",
    "score_symbol_list",
    # materialize
    "_GIT_TIMEOUT_SECONDS",
    "_MAX_DIFF_BYTES",
    "AgentState",
    "_apply_diff",
    "_capture_workspace_diff",
    "_create_fresh_checkout",
    "_is_git_repo",
    "_materialize_workspace",
    "_run_git",
    # result
    "ALLOWED_MATERIALIZATION",
    "ALLOWED_VERDICTS",
    "DEFAULT_FBETA_BETA",
    "DEFAULT_IR_FAMILY",
    "PASS_THRESHOLD",
    "SCORER_FAMILIES",
    "Scorer",
    "ScoreResult",
    "_read_fbeta_beta",
    "_select_ir_family",
    "read_task_metadata",
    "read_task_verification",
    "scorer_accepts_agent_state",
    "scorer_accepts_low_confidence_threshold",
    # sandbox
    "_SAFE_ENV_KEYS",
    "_TOKEN_PATTERN",
    "COPYTREE_IGNORE",
    "SCORE_TIMEOUT_SECONDS",
    "_run_in_sandbox",
    "_safe_env",
    "_SandboxRun",
    "_thread_env_overrides",
    "sanitize_secrets",
    "scorer_env_override",
    # scorers
    "_IR_LIST_ANSWER_TYPES",
    "_MAX_GROUND_TRUTH_BYTES",
    "_ORACLE_TYPE_SCORERS",
    "_WEIGHT_TOLERANCE",
    "VALID_REWARD_TYPES",
    "ArtifactScorer",
    "BinaryScorer",
    "CheckpointScorer",
    "ContinuousScorer",
    "DualScorer",
    "OracleChecksScorer",
    "_cli_main",
    "_find_answer_file",
    "_load_json_file",
    "_parse_float_score",
    "_safe_leg_score",
    "get_scorer",
    "score_task_output",
    "validate_ground_truth",
]
