"""Experiment data models — runtime state for eval experiments."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ExperimentConfig:
    """A single configuration to evaluate (e.g., 'baseline' or 'with-mcp').

    ``allowed_tools`` / ``disallowed_tools`` restrict which tools the
    agent is allowed to call during this config's runs. Semantics mirror
    the underlying CLI (Claude's ``--allowedTools`` / ``--disallowedTools``
    / ``--tools``). Set ``allowed_tools=[]`` to disable all built-in tools
    for an MCP-only comparison — MCP tools are still reachable because
    they come from ``mcp_config``.

    ``mcp_mode`` controls how the executor restricts the tool surface
    when ``mcp_config`` is set (see :mod:`codeprobe.core.mcp_policy`):

    * ``"strict"`` (default): MCP tools + ``Write`` only; ``Grep``,
      ``Bash``, ``Glob`` and ``Read`` are blocked. Pure MCP signal.
    * ``"pragmatic"``: MCP tools + ``Read`` + ``Write``; ``Grep``,
      ``Bash``, ``Glob`` are blocked.
    * ``"loose"``: dual-surface (mirrors pre-0.9.0 behavior); emits a
      runtime warning that comparison validity is compromised.

    Explicit ``allowed_tools`` or ``disallowed_tools`` on the config
    override ``mcp_mode`` — the user-supplied surface always wins.
    """

    label: str
    agent: str = "claude"
    model: str | None = None
    permission_mode: str = "default"
    mcp_config: dict | None = None
    allowed_tools: list[str] | None = None
    disallowed_tools: list[str] | None = None
    mcp_mode: str = "strict"
    instruction_variant: str | None = None
    preambles: tuple[str, ...] = ()
    reward_type: str = "binary"
    max_turns: int | None = None
    # Source-isolation mode for sg-only / sg-hybrid runs.
    #
    # * ``"off"`` (default; codeprobe-2nw2.4): source visible — current
    #   default for back-compat with non-sg-only configs.
    # * ``"hide"`` (codeprobe-jf28): local source stashed for the
    #   duration of the agent run, restored before scoring. Pair with
    #   ``--preamble sourcegraph`` whose v2 body declares "Local source
    #   files are not present." Use for oracle / symbol-reference tasks
    #   whose verifier reads an agent-written text answer.
    # * ``"scaffold"`` (codeprobe-2nw2): local source stashed AND
    #   0-byte placeholder files left at the tracked extensions so the
    #   agent can write edits to known paths via MCP-only reads. The
    #   ``__exit__`` overlay merges agent edits on top of the restored
    #   source before scoring runs. Use for SDLC code-edit tasks.
    #
    # Legacy boolean values are accepted by the loader for back-compat:
    # ``True`` → ``"hide"``, ``False`` → ``"off"``.
    hide_local_source: Literal["off", "hide", "scaffold"] = "off"
    # Confidence below which ArtifactScorer logs a warning about
    # low-confidence ground truth (codeprobe-kdng). Policy threshold,
    # not a score adjustment — tune per config to change sensitivity
    # without editing scorer code. Default reproduces the historical
    # hardcoded value in core/scoring/scorers.py.
    low_confidence_threshold: float = 0.5
    extra: dict = field(default_factory=dict)

    def __repr__(self) -> str:
        # Lazy import: config/__init__.py → loader.py → this module (circular)
        from codeprobe.config.redact import redact_mcp_headers

        redacted_mcp = redact_mcp_headers(self.mcp_config)
        return (
            f"ExperimentConfig(label={self.label!r}, agent={self.agent!r}, "
            f"model={self.model!r}, permission_mode={self.permission_mode!r}, "
            f"mcp_config={redacted_mcp!r}, "
            f"allowed_tools={self.allowed_tools!r}, "
            f"disallowed_tools={self.disallowed_tools!r}, "
            f"mcp_mode={self.mcp_mode!r}, "
            f"instruction_variant={self.instruction_variant!r}, "
            f"preambles={self.preambles!r}, reward_type={self.reward_type!r}, "
            f"max_turns={self.max_turns!r}, "
            f"hide_local_source={self.hide_local_source!r}, "
            f"low_confidence_threshold={self.low_confidence_threshold!r}, "
            f"extra={self.extra!r})"
        )


@dataclass(frozen=True)
class DualScoringDetails:
    """Dual scoring details capturing direct and artifact-based scores.

    Note: This dataclass is a typed view over the plain dict stored in
    ``CompletedTask.scoring_details``. The on-the-wire representation remains
    a ``dict`` to preserve checkpoint serialization compatibility; use
    :meth:`from_dict` / :meth:`to_dict` to convert between the two.
    """

    score_direct: float = 0.0
    score_artifact: float = 0.0
    passed_direct: bool = False
    passed_artifact: bool = False
    scoring_policy: str = ""
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DualScoringDetails:
        """Build an instance from a plain dict, tolerating missing keys.

        Booleans are parsed strictly: ``bool("False")`` is ``True`` in
        Python, so serialized string forms like ``"False"``/``"false"``
        are recognized and mapped to False. Unknown non-bool types fall
        back to ``False``.
        """

        def _as_bool(value: Any) -> bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in {"true", "1", "yes", "y"}
            if isinstance(value, (int, float)):
                return bool(value)
            return False

        known = {
            "score_direct",
            "score_artifact",
            "passed_direct",
            "passed_artifact",
            "scoring_policy",
            "extra",
        }
        extra = dict(d.get("extra", {}))
        for key, value in d.items():
            if key not in known:
                extra[key] = value
        return cls(
            score_direct=float(d.get("score_direct", 0.0)),
            score_artifact=float(d.get("score_artifact", 0.0)),
            passed_direct=_as_bool(d.get("passed_direct", False)),
            passed_artifact=_as_bool(d.get("passed_artifact", False)),
            scoring_policy=str(d.get("scoring_policy", "")),
            extra=extra,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict round-trippable through :meth:`from_dict`."""
        return asdict(self)


@dataclass(frozen=True)
class CompletedTask:
    """Result of running a single task under a single configuration."""

    task_id: str
    automated_score: float
    repeat_index: int = 0
    # Trial outcome taxonomy (codeprobe-8up):
    #   'completed' — scoring ran end-to-end.
    #   'failed'    — terminal agent failure (e.g. error_max_turns): a
    #                 genuine 0.0-reward measurement, KEPT on checkpoint
    #                 resume.
    #   'error'     — infra casualty (quota stub, crash, no result
    #                 record): RETRIED on checkpoint resume.
    status: str = "completed"
    duration_seconds: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cost_usd: float | None = None
    cost_model: str = "unknown"
    cost_source: str = "unavailable"
    tool_call_count: int | None = None
    # Per-tool usage breakdown (e.g. {"Read": 5,
    # "mcp__sourcegraph__keyword_search": 2}). None when not captured.
    tool_use_by_name: dict[str, int] | None = None
    # CLI result-record fields, persisted for ALL trials — success and
    # error — so cap-retune analysis can read the turn distribution of
    # finished trials directly instead of proxying with tool_call_count
    # (codeprobe-8up). Additive: None when the adapter had no record.
    num_turns: int | None = None
    result_subtype: str | None = None
    duration_api_ms: int | None = None
    error_category: str | None = None
    # Plain-dict form of the offered tool surface (codeprobe-9p6), as
    # produced by ``McpInitManifest.to_dict()``. None when the adapter had
    # no streaming transcript to parse. Stored as a dict (not the typed
    # manifest) so checkpoint asdict()/round-trip stays trivial.
    mcp_init: dict | None = None
    scoring_details: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ConfigResults:
    """All results for a single configuration."""

    config: str
    completed: list[CompletedTask] = field(default_factory=list)


@dataclass(frozen=True)
class Experiment:
    """Top-level experiment with metadata and configuration matrix."""

    name: str
    description: str = ""
    configs: list[ExperimentConfig] = field(default_factory=list)
    tasks_dir: str = "tasks"
    task_ids: tuple[str, ...] = ()
    # Bias-detection thresholds (codeprobe-kdng) — see
    # core/bias_detection.py's overshipping detector. Experiment-scoped
    # (not per-config) because the detector compares two configs'
    # results against one shared sensitivity bar. Defaults reproduce the
    # historical hardcoded values so behavior is unchanged unless a user
    # opts into different sensitivity.
    #
    # * ``bias_overshipping_recall_min``: both configs must reach this
    #   recall before an over-shipping comparison is even considered.
    # * ``bias_overshipping_low_precision_max``: the over-shipper's
    #   precision must be at or below this to flag.
    # * ``bias_overshipping_precision_gap_min``: minimum precision delta
    #   between the two configs required to flag.
    bias_overshipping_recall_min: float = 0.95
    bias_overshipping_low_precision_max: float = 0.5
    bias_overshipping_precision_gap_min: float = 0.3
    # Anchor for `run --out <dir>` (codeprobe-xcue): when a run relocates its
    # writes (runs/, checkpoints, trace.db) away from the experiment
    # directory, the absolute destination is recorded here so a plain
    # `codeprobe interpret <exp_dir>` — with zero extra flags — can still
    # find the relocated results.json files. ``None`` (the default) means
    # results live under the experiment directory itself, exactly as before
    # this field existed.
    results_base_dir: str | None = None
