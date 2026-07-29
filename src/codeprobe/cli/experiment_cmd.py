"""codeprobe experiment — manage eval experiments."""

from __future__ import annotations

import dataclasses
import json
import re
import statistics
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import click

from codeprobe.adapters.protocol import quarantine_message
from codeprobe.analysis.stats import partition_reward_population
from codeprobe.analysis.validity import is_infra_failure
from codeprobe.cli._output_helpers import emit_envelope, resolve_explicit_mode
from codeprobe.cli.errors import DiagnosticError, PrescriptiveError
from codeprobe.config.defaults import resolve_experiment_config
from codeprobe.core.experiment import (
    create_experiment_dir,
    load_config_results,
    load_experiment,
    remove_experiment_config,
    save_experiment,
    update_experiment_config,
)
from codeprobe.core.registry import available
from codeprobe.core.registry import resolve as resolve_agent
from codeprobe.models.experiment import (
    Experiment,
    ExperimentConfig,
)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_UNSAFE_COMPONENT_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_component_suggestion(value: str, fallback: str) -> str:
    """Mechanically sanitize *value* into a safe path component.

    Used as the ``next_try_value`` on unsafe-name PrescriptiveErrors so an
    agent can retry without further reasoning. Falls back to *fallback*
    when nothing usable survives sanitization.
    """
    cleaned = _UNSAFE_COMPONENT_CHARS.sub("-", value).strip("-.")
    return cleaned or fallback


def _unsafe_component_error(
    exc: ValueError, flag: str, value: str, fallback: str
) -> PrescriptiveError:
    """Build the UNSAFE_NAME error for an invalid --name / --label value."""
    return PrescriptiveError(
        code="UNSAFE_NAME",
        message=str(exc),
        next_try_flag=flag,
        next_try_value=_safe_component_suggestion(value, fallback),
        exit_code=1,
        detail={"value": value},
    )


def _experiment_load_error(exp_dir: Path, exc: Exception) -> DiagnosticError:
    """Build the EXPERIMENT_INVALID error for an unloadable experiment.json."""
    return DiagnosticError(
        code="EXPERIMENT_INVALID",
        message=f"failed to load experiment at {exp_dir}: {exc}",
        diagnose_cmd=f"cat {exp_dir / 'experiment.json'}",
        exit_code=1,
        detail={"experiment_dir": str(exp_dir)},
    )


def _resolve_exp_dir(path: str) -> Path:
    """Resolve a user-supplied path token to the experiment directory.

    Accepts the same tokens as ``experiment init``: an explicit experiment
    directory (one containing ``experiment.json``) is used as-is; any other
    path is treated as a repo root and the shared resolver discovers the
    experiment under ``<path>/.codeprobe/`` — both the direct
    ``experiment.json`` layout written by ``experiment init
    --non-interactive`` and the named-subdir layout.

    On failure the typed CLI errors from
    :func:`codeprobe.config.defaults.resolve_experiment_config`
    (``NO_EXPERIMENT`` / ``AMBIGUOUS_EXPERIMENT``) propagate to
    :class:`codeprobe.cli._error_handler.CodeprobeGroup` for rendering.
    """
    base_dir = Path(path)
    if (base_dir / "experiment.json").is_file():
        return base_dir
    config_path, _ = resolve_experiment_config(base_dir)
    return config_path.parent


def experiment_init(
    path: str,
    name: str,
    description: str,
    non_interactive: bool = False,
    json_flag: bool = False,
    no_json_flag: bool = False,
    json_lines_flag: bool = False,
) -> None:
    """Create a new experiment directory.

    In ``non_interactive`` mode the experiment is materialized inside the
    target's ``.codeprobe/`` directory so that ``.codeprobe/experiment.json``
    is the canonical default location, matching the documented golden path.
    """
    mode = resolve_explicit_mode(
        "experiment init", json_flag, no_json_flag, json_lines_flag
    )
    emit_json = mode.mode != "pretty"
    base_dir = Path(path)
    if non_interactive:
        # Default location is <path>/.codeprobe/, with experiment.json
        # written directly inside it (no nested name subdir).
        from codeprobe.core.experiment import _validate_path_component
        from codeprobe.core.repo_hygiene import ensure_codeprobe_excluded

        try:
            _validate_path_component(name, "experiment name")
        except ValueError as exc:
            raise _unsafe_component_error(exc, "--name", name, "default") from exc

        codeprobe_dir = base_dir / ".codeprobe"
        codeprobe_dir.mkdir(exist_ok=True)
        ensure_codeprobe_excluded(base_dir)

        exp_json = codeprobe_dir / "experiment.json"
        if exp_json.exists():
            raise DiagnosticError(
                code="EXPERIMENT_EXISTS",
                message=f"experiment already exists at {exp_json}",
                diagnose_cmd=f"codeprobe experiment status {base_dir}",
                exit_code=1,
                detail={"experiment_path": str(exp_json)},
            )

        experiment = Experiment(name=name, description=description)
        try:
            save_experiment(codeprobe_dir, experiment)
        except ValueError as exc:
            raise _unsafe_component_error(exc, "--name", name, "default") from exc
        (codeprobe_dir / "tasks").mkdir(exist_ok=True)

        if emit_json:
            emit_envelope(
                command="experiment init",
                data={
                    "experiment_dir": str(codeprobe_dir),
                    "name": name,
                    "created": True,
                },
            )
            return
        click.echo(f"Experiment '{name}' created at {codeprobe_dir}/")
        click.echo("  Tasks: 0 (add tasks to tasks/ directory)")
        click.echo(
            "  Configs: 0 (use 'codeprobe experiment add-config' to define "
            "configurations)"
        )
        return

    exp_dir = base_dir / name

    if exp_dir.exists():
        raise DiagnosticError(
            code="EXPERIMENT_EXISTS",
            message=f"experiment '{name}' already exists at {exp_dir}",
            diagnose_cmd=f"codeprobe experiment status {exp_dir}",
            exit_code=1,
            detail={"experiment_path": str(exp_dir)},
        )

    experiment = Experiment(
        name=name,
        description=description,
    )

    try:
        created = create_experiment_dir(base_dir, experiment)
    except ValueError as exc:
        raise _unsafe_component_error(exc, "--name", name, "default") from exc

    if emit_json:
        emit_envelope(
            command="experiment init",
            data={
                "experiment_dir": str(created),
                "name": name,
                "created": True,
            },
        )
        return
    click.echo(f"Experiment '{name}' created at {created}/")
    click.echo("  Tasks: 0 (add tasks to tasks/ directory)")
    click.echo(
        "  Configs: 0 (use 'codeprobe experiment add-config' to define configurations)"
    )


def _interactive_mcp_selection() -> str | None:
    """Offer interactive MCP config selection when available.

    Returns a file path string if the user selects a config, or None to skip.
    """
    from codeprobe.core.mcp_discovery import discover_mcp_configs

    discovered = discover_mcp_configs()
    if not discovered:
        return None

    click.echo()
    click.echo("Discovered MCP configurations:")
    for i, (p, servers) in enumerate(discovered, 1):
        click.echo(f"  {i}. {p}  ({len(servers)} servers)")
        for s in servers:
            click.echo(f"     - {s}")
    click.echo(f"  {len(discovered) + 1}. Skip (no MCP config)")
    click.echo()

    choice = click.prompt(
        "Select MCP config",
        type=click.IntRange(1, len(discovered) + 1),
        default=len(discovered) + 1,
    )
    if choice <= len(discovered):
        return str(discovered[choice - 1][0])
    return None


def _parse_mcp_config(raw: str) -> dict:
    """Parse ``--mcp-config`` as inline JSON or a path to a JSON file.

    Raises :class:`PrescriptiveError` (``INVALID_MCP_CONFIG``) when the
    value is neither valid inline JSON nor a readable JSON file, or when
    the decoded value is not a JSON object.
    """
    parsed: object
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        mcp_path = Path(raw).expanduser().resolve()
        if not mcp_path.is_file():
            raise PrescriptiveError(
                code="INVALID_MCP_CONFIG",
                message="--mcp-config is not valid JSON or a file path",
                next_try_flag="--mcp-config",
                next_try_value="<path-to-valid-mcp-config.json>",
                exit_code=1,
                detail={"mcp_config": raw},
            ) from None
        try:
            parsed = json.loads(mcp_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PrescriptiveError(
                code="INVALID_MCP_CONFIG",
                message=f"--mcp-config file {mcp_path} is not valid JSON: {exc}",
                next_try_flag="--mcp-config",
                next_try_value="<path-to-valid-mcp-config.json>",
                exit_code=1,
                detail={"mcp_config_path": str(mcp_path)},
            ) from exc

    if not isinstance(parsed, dict):
        raise PrescriptiveError(
            code="INVALID_MCP_CONFIG",
            message="--mcp-config must decode to a JSON object",
            next_try_flag="--mcp-config",
            next_try_value="<path-to-valid-mcp-config.json>",
            exit_code=1,
            detail={"mcp_config": raw},
        )
    return parsed


def _next_free_label(label: str, existing: list[str]) -> str:
    """Return the first ``<label>-N`` (N >= 2) not already in *existing*."""
    n = 2
    while f"{label}-{n}" in existing:
        n += 1
    return f"{label}-{n}"


def experiment_add_config(
    path: str,
    label: str,
    agent: str,
    model: str | None,
    permission_mode: str,
    mcp_config_str: str | None,
    instruction_variant: str | None = None,
    preambles: tuple[str, ...] = (),
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    mcp_mode: str = "strict",
    hide_local_source: str = "off",
    json_flag: bool = False,
    no_json_flag: bool = False,
    json_lines_flag: bool = False,
) -> None:
    """Add a configuration to an existing experiment.

    ``hide_local_source`` must be one of ``"off"``, ``"hide"``, or
    ``"scaffold"`` (see :class:`ExperimentConfig`). Passed straight
    through to the persisted experiment.json; the loader handles
    legacy boolean values for back-compat on subsequent reads.
    """
    mode = resolve_explicit_mode(
        "experiment add-config", json_flag, no_json_flag, json_lines_flag
    )
    emit_json = mode.mode != "pretty"

    # A config that can never run must not be persisted: run preflight
    # refuses quarantined adapters (codeprobe-f7rl.27), so adding such an
    # arm would only store a guaranteed-refusal config. Unknown agent
    # names pass through unchanged here — backend validation is a
    # separate concern (codeprobe-f7rl.25).
    try:
        candidate_adapter: object | None = resolve_agent(agent)
    except KeyError:
        candidate_adapter = None
    if candidate_adapter is not None and getattr(
        candidate_adapter, "quarantined", False
    ):
        click.echo(f"Error: {quarantine_message(agent)}", err=True)
        # lint-exempt: f7rl.27 pins SystemExit(1), the add-config echo+exit style
        raise SystemExit(1)

    exp_dir = _resolve_exp_dir(path)

    try:
        experiment = load_experiment(exp_dir)
    except (FileNotFoundError, ValueError) as exc:
        raise _experiment_load_error(exp_dir, exc) from exc

    # Check for duplicate label
    existing_labels = [c.label for c in experiment.configs]
    if label in existing_labels:
        raise PrescriptiveError(
            code="DUPLICATE_CONFIG_LABEL",
            message=(
                f"configuration '{label}' already exists in experiment "
                f"'{experiment.name}'"
            ),
            next_try_flag="--label",
            next_try_value=_next_free_label(label, existing_labels),
            exit_code=1,
            detail={"label": label, "existing_labels": existing_labels},
        )

    # Refuse unknown agent backends at authoring time — a typo here would
    # otherwise persist to experiment.json (codeprobe-f7rl.25). available()
    # merges builtins with entry-point-registered third-party adapters.
    known_agents = available()
    if agent not in known_agents:
        raise PrescriptiveError(
            code="UNKNOWN_BACKEND",
            message=(
                f"unknown agent backend {agent!r}. "
                f"Available: {', '.join(known_agents)}"
            ),
            next_try_flag="--agent",
            next_try_value="claude",
            exit_code=1,
            detail={"agent": agent, "available": known_agents},
        )

    # Parse MCP config — offer interactive discovery when omitted in a TTY
    mcp_config: dict | None = None
    if mcp_config_str:
        mcp_config = _parse_mcp_config(mcp_config_str)
    elif sys.stderr.isatty():
        mcp_config_str = _interactive_mcp_selection()
        if mcp_config_str:
            mcp_path = Path(mcp_config_str).expanduser().resolve()
            if mcp_path.is_file():
                mcp_config = json.loads(mcp_path.read_text(encoding="utf-8"))

    new_config = ExperimentConfig(
        label=label,
        agent=agent,
        model=model,
        permission_mode=permission_mode,
        mcp_config=mcp_config,
        instruction_variant=instruction_variant,
        preambles=preambles,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        mcp_mode=mcp_mode,
        hide_local_source=cast(Literal["off", "hide", "scaffold"], hide_local_source),
    )

    # Validate the label is a safe path component
    from codeprobe.core.experiment import _validate_path_component

    try:
        _validate_path_component(label, "config label")
    except ValueError as exc:
        raise _unsafe_component_error(exc, "--label", label, "baseline") from exc

    # Field-generic copy: any future Experiment field (like task_ids, which
    # scopes run to the mined task set) survives without this call site
    # knowing about it.
    updated = dataclasses.replace(
        experiment, configs=[*experiment.configs, new_config]
    )
    save_experiment(exp_dir, updated)

    # Create runs directory for this config
    (exp_dir / "runs" / label).mkdir(parents=True, exist_ok=True)

    if emit_json:
        emit_envelope(
            command="experiment add-config",
            data={
                "label": label,
                "agent": agent,
                "model": model,
                "config_count": len(updated.configs),
            },
        )
        return
    click.echo(f"Configuration '{label}' added to experiment '{experiment.name}'")
    click.echo(f"  Agent: {agent}")
    click.echo(f"  Model: {model or '(not specified)'}")
    click.echo(f"  Total configs: {len(updated.configs)}")


def _config_not_found_error(
    experiment: Experiment,
    label: str,
) -> PrescriptiveError:
    labels = [config.label for config in experiment.configs]
    return PrescriptiveError(
        code="CONFIG_NOT_FOUND",
        message=(
            f"configuration '{label}' does not exist in experiment "
            f"'{experiment.name}'"
        ),
        next_try_flag="--label",
        next_try_value=labels[0] if labels else "<label>",
        exit_code=1,
        detail={"label": label, "existing_labels": labels},
    )


def _get_config_or_raise(
    experiment: Experiment,
    label: str,
) -> ExperimentConfig:
    for config in experiment.configs:
        if config.label == label:
            return config
    raise _config_not_found_error(experiment, label)


def _no_config_changes_error(
    experiment: Experiment,
    label: str,
) -> PrescriptiveError:
    existing_labels = [config.label for config in experiment.configs]
    return PrescriptiveError(
        code="NO_CONFIG_CHANGES",
        message=f"no changes requested for configuration '{label}'",
        next_try_flag="--new-label",
        next_try_value=_next_free_label(label, existing_labels),
        exit_code=1,
        detail={"label": label},
    )


def _duplicate_config_label_error(
    experiment: Experiment,
    label: str,
    flag: str = "--label",
) -> PrescriptiveError:
    existing_labels = [config.label for config in experiment.configs]
    return PrescriptiveError(
        code="DUPLICATE_CONFIG_LABEL",
        message=(
            f"configuration '{label}' already exists in experiment "
            f"'{experiment.name}'"
        ),
        next_try_flag=flag,
        next_try_value=_next_free_label(label, existing_labels),
        exit_code=1,
        detail={"label": label, "existing_labels": existing_labels},
    )


def _validate_updated_agent(agent: str) -> None:
    try:
        candidate_adapter: object | None = resolve_agent(agent)
    except KeyError:
        candidate_adapter = None
    if candidate_adapter is not None and getattr(
        candidate_adapter, "quarantined", False
    ):
        click.echo(f"Error: {quarantine_message(agent)}", err=True)
        # lint-exempt: keep add-config's quarantined-backend exit contract.
        raise SystemExit(1)

    known_agents = available()
    if agent not in known_agents:
        raise PrescriptiveError(
            code="UNKNOWN_BACKEND",
            message=(
                f"unknown agent backend {agent!r}. "
                f"Available: {', '.join(known_agents)}"
            ),
            next_try_flag="--agent",
            next_try_value="claude",
            exit_code=1,
            detail={"agent": agent, "available": known_agents},
        )


def _load_experiment_for_update(path: str) -> tuple[Path, Experiment]:
    exp_dir = _resolve_exp_dir(path)
    try:
        return exp_dir, load_experiment(exp_dir)
    except (FileNotFoundError, ValueError) as exc:
        raise _experiment_load_error(exp_dir, exc) from exc


def experiment_update_config(
    path: str,
    label: str,
    new_label: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    permission_mode: str | None = None,
    mcp_config_str: str | None = None,
    instruction_variant: str | None = None,
    preambles: tuple[str, ...] = (),
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    mcp_mode: str | None = None,
    hide_local_source: str | None = None,
    json_flag: bool = False,
    no_json_flag: bool = False,
    json_lines_flag: bool = False,
) -> None:
    """Update an existing experiment configuration in place."""
    mode = resolve_explicit_mode(
        "experiment update-config", json_flag, no_json_flag, json_lines_flag
    )
    emit_json = mode.mode != "pretty"
    exp_dir, experiment = _load_experiment_for_update(path)
    current = _get_config_or_raise(experiment, label)

    changes: dict[str, Any] = {}
    if new_label is not None:
        from codeprobe.core.experiment import _validate_path_component

        try:
            _validate_path_component(new_label, "config label")
        except ValueError as exc:
            raise _unsafe_component_error(
                exc, "--new-label", new_label, "baseline"
            ) from exc
        if new_label != current.label and new_label in [
            config.label for config in experiment.configs
        ]:
            raise _duplicate_config_label_error(
                experiment, new_label, flag="--new-label"
            )
        changes["label"] = new_label
    if agent is not None:
        _validate_updated_agent(agent)
        changes["agent"] = agent
    if model is not None:
        changes["model"] = model
    if permission_mode is not None:
        changes["permission_mode"] = permission_mode
    if mcp_config_str is not None:
        changes["mcp_config"] = _parse_mcp_config(mcp_config_str)
    if instruction_variant is not None:
        changes["instruction_variant"] = instruction_variant
    if preambles:
        changes["preambles"] = preambles
    if allowed_tools is not None:
        changes["allowed_tools"] = allowed_tools
    if disallowed_tools is not None:
        changes["disallowed_tools"] = disallowed_tools
    if mcp_mode is not None:
        changes["mcp_mode"] = mcp_mode
    if hide_local_source is not None:
        changes["hide_local_source"] = cast(
            Literal["off", "hide", "scaffold"],
            hide_local_source,
        )

    replacement = dataclasses.replace(current, **changes)
    if replacement == current:
        raise _no_config_changes_error(experiment, label)

    try:
        updated = update_experiment_config(exp_dir, label, replacement)
    except KeyError as exc:
        raise _config_not_found_error(experiment, label) from exc
    except ValueError as exc:
        message = str(exc)
        if "already exists" in message and "configuration" in message:
            raise _duplicate_config_label_error(
                experiment, replacement.label, flag="--new-label"
            ) from exc
        raise DiagnosticError(
            code="CONFIG_RUN_DIR_EXISTS",
            message=message,
            diagnose_cmd=f"ls -la {exp_dir / 'runs'}",
            exit_code=1,
            detail={
                "experiment_dir": str(exp_dir),
                "label": label,
                "new_label": replacement.label,
            },
        ) from exc

    if emit_json:
        emit_envelope(
            command="experiment update-config",
            data={
                "old_label": label,
                "label": replacement.label,
                "config_count": len(updated.configs),
            },
        )
        return

    click.echo(f"Configuration '{label}' updated in experiment '{experiment.name}'")
    click.echo(f"  Label: {replacement.label}")
    click.echo(f"  Agent: {replacement.agent}")
    click.echo(f"  Model: {replacement.model or '(not specified)'}")
    click.echo(f"  Total configs: {len(updated.configs)}")


def experiment_remove_config(
    path: str,
    label: str,
    yes: bool = False,
    json_flag: bool = False,
    no_json_flag: bool = False,
    json_lines_flag: bool = False,
) -> None:
    """Remove an experiment configuration after an explicit confirmation flag."""
    mode = resolve_explicit_mode(
        "experiment remove-config", json_flag, no_json_flag, json_lines_flag
    )
    emit_json = mode.mode != "pretty"
    exp_dir, experiment = _load_experiment_for_update(path)
    config = _get_config_or_raise(experiment, label)
    run_dir = exp_dir / "runs" / config.label
    run_dir_exists = run_dir.exists() or run_dir.is_symlink()

    if not yes:
        if emit_json:
            emit_envelope(
                command="experiment remove-config",
                data={
                    "label": label,
                    "deleted": False,
                    "dry_run": True,
                    "run_dir": str(run_dir),
                    "run_dir_exists": run_dir_exists,
                },
            )
            return
        click.echo(
            f"Configuration '{label}' would be removed from "
            f"experiment '{experiment.name}'"
        )
        click.echo(f"  Agent: {config.agent}")
        click.echo(f"  Model: {config.model or '(not specified)'}")
        click.echo(f"  Run artifacts: {run_dir} ({'exists' if run_dir_exists else 'absent'})")
        click.echo(
            "Dry run: nothing removed. Re-run with --yes to remove this "
            "configuration and its run artifacts."
        )
        return

    try:
        updated = remove_experiment_config(exp_dir, label)
    except KeyError as exc:
        raise _config_not_found_error(experiment, label) from exc

    if emit_json:
        emit_envelope(
            command="experiment remove-config",
            data={
                "label": label,
                "deleted": True,
                "run_dir": str(run_dir),
                "run_dir_deleted": run_dir_exists,
                "config_count": len(updated.configs),
            },
        )
        return

    click.echo(f"Configuration '{label}' removed from experiment '{experiment.name}'")
    if run_dir_exists:
        click.echo(f"  Deleted run artifacts: {run_dir}")
    click.echo(f"  Total configs: {len(updated.configs)}")


def experiment_validate(
    path: str,
    *,
    allow_low_confidence: bool = False,
    json_flag: bool = False,
    no_json_flag: bool = False,
    json_lines_flag: bool = False,
) -> None:
    """Validate experiment structure and readiness.

    By default, refuses tasks whose ``confidence.json#score`` is below the
    promotion gate (``mining.confidence.DEFAULT_THRESHOLD``). Pass
    ``allow_low_confidence=True`` to keep low-confidence tasks in the run
    plan — useful for exploratory experiments where reduced confidence is
    acceptable.
    """
    from codeprobe.mining.confidence import (
        DEFAULT_THRESHOLD,
        load_confidence_file,
        score_task_confidence,
    )

    mode = resolve_explicit_mode(
        "experiment validate", json_flag, no_json_flag, json_lines_flag
    )
    emit_json = mode.mode != "pretty"
    exp_dir = _resolve_exp_dir(path)

    try:
        experiment = load_experiment(exp_dir)
    except (FileNotFoundError, ValueError) as exc:
        raise _experiment_load_error(exp_dir, exc) from exc

    errors: list[str] = []
    warnings: list[str] = []

    # Discover tasks from the tasks directory
    tasks_dir = exp_dir / experiment.tasks_dir
    task_ids: list[str] = []
    if tasks_dir.is_dir():
        task_ids = sorted(
            d.name
            for d in tasks_dir.iterdir()
            if d.is_dir() and (d / "instruction.md").exists()
        )

    if not task_ids:
        errors.append("No tasks found. Add task directories to tasks/.")

    quarantined: list[str] = []
    for task_id in task_ids:
        task_dir = tasks_dir / task_id
        if not (task_dir / "tests" / "test.sh").exists():
            warnings.append(
                f"Task '{task_id}' has no tests/test.sh (automated scoring unavailable)"
            )
        # Confidence gate: prefer cached confidence.json, fall back to live score.
        score = load_confidence_file(task_dir)
        if score is None:
            score = score_task_confidence(task_dir)
        if score.score < DEFAULT_THRESHOLD:
            msg = (
                f"Task '{task_id}' confidence={score.score:.2f} "
                f"< {DEFAULT_THRESHOLD} (promotion gate)"
            )
            if allow_low_confidence:
                warnings.append(msg + " — admitted via --allow-low-confidence")
            else:
                quarantined.append(task_id)
                errors.append(
                    msg + " — quarantined; pass --allow-low-confidence to override"
                )

    if not experiment.configs:
        errors.append(
            "No configurations defined. Use 'add-config' to add at least one."
        )

    admitted_tasks = [t for t in task_ids if t not in quarantined]
    total_runs = len(admitted_tasks) * len(experiment.configs)

    if errors:
        status = "not_ready"
    elif warnings:
        status = "ready_with_warnings"
    else:
        status = "ready"

    if emit_json:
        emit_envelope(
            command="experiment validate",
            ok=not errors,
            exit_code=1 if errors else 0,
            data={
                "experiment": experiment.name,
                "task_count": len(task_ids),
                "quarantined_count": len(quarantined),
                "config_count": len(experiment.configs),
                "total_runs": total_runs,
                "errors": errors,
                "warnings": warnings,
                "status": status,
            },
        )
        if errors:
            # lint-exempt: envelope already emitted; exit code only.
            raise SystemExit(1)
        return

    click.echo(f"Experiment: {experiment.name}")
    click.echo(f"  Tasks: {len(task_ids)} ({len(quarantined)} quarantined)")
    click.echo(f"  Configurations: {len(experiment.configs)}")
    click.echo(f"  Total runs needed: {total_runs}")

    if errors:
        click.echo(f"\n  ERRORS ({len(errors)}):")
        for e in errors:
            click.echo(f"    - {e}")
    if warnings:
        click.echo(f"\n  WARNINGS ({len(warnings)}):")
        for w in warnings:
            click.echo(f"    - {w}")

    if not errors and not warnings:
        click.echo("\n  Status: READY to run")
    elif not errors:
        click.echo(f"\n  Status: READY (with {len(warnings)} warnings)")
    else:
        click.echo(f"\n  Status: NOT READY ({len(errors)} errors)")
        # lint-exempt: report already rendered; exit code only.
        raise SystemExit(1)


def experiment_status(
    path: str,
    json_flag: bool = False,
    no_json_flag: bool = False,
    json_lines_flag: bool = False,
) -> None:
    """Report completion status per configuration."""
    from codeprobe.mining.confidence import (
        confidence_histogram,
        load_confidence_file,
        score_task_confidence,
    )

    mode = resolve_explicit_mode(
        "experiment status", json_flag, no_json_flag, json_lines_flag
    )
    emit_json = mode.mode != "pretty"
    exp_dir = _resolve_exp_dir(path)

    try:
        experiment = load_experiment(exp_dir)
    except (FileNotFoundError, ValueError) as exc:
        raise _experiment_load_error(exp_dir, exc) from exc

    tasks_dir = exp_dir / experiment.tasks_dir
    task_ids: list[str] = []
    if tasks_dir.is_dir():
        task_ids = sorted(
            d.name
            for d in tasks_dir.iterdir()
            if d.is_dir() and (d / "instruction.md").exists()
        )

    total_tasks = len(task_ids)

    # Per-config completion rows — computed once, rendered per mode.
    config_rows: list[dict[str, Any]] = []
    for cfg in experiment.configs:
        completed = 0
        distinct_done = 0
        avg_score: float | None = None

        try:
            results = load_config_results(exp_dir, cfg.label)
            completed = len(results.completed)
            # `completed` counts trials (tasks x repeats); completion is
            # judged on distinct task_ids so repeated runs can finish
            # (codeprobe-f7rl.7).
            distinct_done = len({t.task_id for t in results.completed})
            automated_scores = [
                t.automated_score
                for t in results.completed
                if t.automated_score is not None
            ]
            if automated_scores:
                avg_score = statistics.mean(automated_scores)
        except FileNotFoundError:
            pass

        # `completed` counts trials (tasks x repeats); completion is judged
        # on distinct task_ids so repeated runs can finish (codeprobe-f7rl.7).
        config_rows.append(
            {
                "label": cfg.label,
                "completed": completed,
                "distinct_done": distinct_done,
                "total": total_tasks,
                "avg_score": avg_score,
                "status": (
                    "complete"
                    if distinct_done == total_tasks and total_tasks > 0
                    else "pending"
                ),
            }
        )

    if emit_json:
        emit_envelope(
            command="experiment status",
            data={
                "experiment": experiment.name,
                "description": experiment.description,
                "total_tasks": total_tasks,
                "configs": config_rows,
            },
        )
        return

    click.echo(f"Experiment: {experiment.name}")
    click.echo(f"  Description: {experiment.description}")
    click.echo(f"  Tasks: {total_tasks}")

    # Confidence histogram across the task set
    if task_ids:
        conf_scores = []
        for tid in task_ids:
            td = tasks_dir / tid
            cached = load_confidence_file(td)
            conf_scores.append(
                cached if cached is not None else score_task_confidence(td)
            )
        hist = confidence_histogram(conf_scores)
        nonzero = {bucket: count for bucket, count in hist.items() if count}
        if nonzero:
            click.echo("  Confidence histogram:")
            for bucket, count in hist.items():
                bar = "#" * count
                click.echo(f"    {bucket:<10} {count:>4} {bar}")
    click.echo()

    if not experiment.configs:
        click.echo("  No configurations defined yet.")
        return

    click.echo(
        f"  {'Configuration':<25} {'Completed':<12} {'Score (avg)':<12} {'Status'}"
    )
    click.echo(f"  {'-' * 25} {'-' * 12} {'-' * 12} {'-' * 10}")

    for row in config_rows:
        avg_score = row["avg_score"]
        completed = row["completed"]
        distinct_done = row["distinct_done"]
        score_str = f"{avg_score:.2f}" if avg_score is not None else "--"
        progress = f"{distinct_done}/{total_tasks}"
        if completed > distinct_done:
            progress += f" ({completed} trials)"
        click.echo(
            f"  {row['label']:<25} {progress:<12} {score_str:<12} {row['status']}"
        )


def experiment_aggregate(
    path: str,
    no_warn: bool = False,
    json_flag: bool = False,
    no_json_flag: bool = False,
    json_lines_flag: bool = False,
) -> None:
    """Aggregate results across configurations into a comparison report.

    When ``no_warn`` is ``True``, bias warnings are suppressed in the
    console output and winner-suppression is disabled — useful for
    scripted aggregation. The structured ``bias_warnings`` array in
    ``aggregate.json`` is always written so downstream tooling still
    sees the signal.
    """
    mode = resolve_explicit_mode(
        "experiment aggregate", json_flag, no_json_flag, json_lines_flag
    )
    emit_json = mode.mode != "pretty"
    exp_dir = _resolve_exp_dir(path)

    try:
        experiment = load_experiment(exp_dir)
    except (FileNotFoundError, ValueError) as exc:
        raise _experiment_load_error(exp_dir, exc) from exc

    if len(experiment.configs) < 1:
        raise DiagnosticError(
            code="NO_CONFIGS",
            message="need at least 1 configuration with results to aggregate",
            diagnose_cmd=f"codeprobe experiment status {path}",
            exit_code=1,
            next_steps=[
                (
                    "Add a configuration",
                    f"codeprobe experiment add-config {path} --label <label>",
                ),
            ],
            detail={"experiment_dir": str(exp_dir)},
        )

    # Load results for each config
    config_results: dict[str, list[dict]] = {}
    completed_by_config: dict[str, list[Any]] = {}
    for cfg in experiment.configs:
        try:
            results = load_config_results(exp_dir, cfg.label)
            completed_by_config[cfg.label] = list(results.completed)
            config_results[cfg.label] = [
                {
                    "task_id": t.task_id,
                    # 0-based repeat number so aggregate consumers can
                    # disaggregate repeated trials (codeprobe-f7rl.7).
                    "repeat_index": t.repeat_index,
                    "automated_score": t.automated_score,
                    "duration_seconds": t.duration_seconds,
                    "cost_usd": t.cost_usd,
                    "input_tokens": t.input_tokens,
                    "output_tokens": t.output_tokens,
                    "cache_read_tokens": t.cache_read_tokens,
                    "cache_creation_tokens": t.cache_creation_tokens,
                    # Oracle metrics surfaced via scoring_details (Option 1
                    # plumbing: precision/recall/f1 don't change scoring,
                    # they just stop being hidden).
                    "scoring_details": dict(t.scoring_details or {}),
                }
                for t in results.completed
            ]
        except FileNotFoundError:
            completed_by_config[cfg.label] = []
            config_results[cfg.label] = []

    # Per-config summaries
    config_summaries: dict[str, dict] = {}
    for cfg_label, cfg_rows in config_results.items():
        # Headline mean/stdev exclude quota casualties (see
        # partition_reward_population); cost/time/token totals below stay over
        # all tasks — structural. The parallel CompletedTask list carries
        # error_category, so the partition runs there rather than on the row
        # dicts (codeprobe-9jxx).
        _cfg_tasks = completed_by_config.get(cfg_label, [])
        reward_tasks, quota_error_count, errored_count = partition_reward_population(
            _cfg_tasks
        )
        infra_failure_count = sum(1 for t in _cfg_tasks if is_infra_failure(t))
        scores = [
            t.automated_score
            for t in reward_tasks
            if t.automated_score is not None
        ]
        costs = [r["cost_usd"] for r in cfg_rows if r.get("cost_usd") is not None]
        times = [
            r["duration_seconds"]
            for r in cfg_rows
            if r.get("duration_seconds") is not None
        ]
        # Raw token counts (codeprobe-oktg). Tasks where the adapter
        # couldn't capture usage contribute None and are excluded from the
        # mean rather than counted as zero — keeps cost-Pareto comparisons
        # honest when only some configs have telemetry.
        input_tokens_per_task = [
            r["input_tokens"]
            for r in cfg_rows
            if r.get("input_tokens") is not None
        ]
        output_tokens_per_task = [
            r["output_tokens"]
            for r in cfg_rows
            if r.get("output_tokens") is not None
        ]
        # codeprobe-e9pr: cache fields surface bulk-reuse and write-through
        # tokens separately. ``input_tokens`` is the uncached input portion;
        # ``cache_read_tokens`` is the bulk re-use; ``cache_creation_tokens``
        # is the write-through cost. cost_usd already accounts for the cache
        # rate inside the adapter — surfacing the raw counts lets cost-Pareto
        # plots reason about cache-hit rates without re-deriving cost.
        cache_read_per_task = [
            r["cache_read_tokens"]
            for r in cfg_rows
            if r.get("cache_read_tokens") is not None
        ]
        cache_creation_per_task = [
            r["cache_creation_tokens"]
            for r in cfg_rows
            if r.get("cache_creation_tokens") is not None
        ]

        # Oracle metrics from scoring_details — only present for tasks scored
        # via the oracle (file_list / symbol_list / etc). Tasks without these
        # fields are excluded from the mean rather than counted as zero.
        def _detail_values(key: str) -> list[float]:
            out: list[float] = []
            for r in cfg_rows:
                v = (r.get("scoring_details") or {}).get(key)
                if isinstance(v, (int, float)):
                    out.append(float(v))
            return out

        precisions = _detail_values("precision")
        recalls = _detail_values("recall")
        f1s = _detail_values("f1")

        # Per-task scorer_family distribution — surfaces which rubric
        # drove each task's reward so reviewers can spot mixed-family
        # configs (e.g. an experiment that opted half its tasks into
        # oracle_overlap_recall would not be comparable to a default
        # oracle_overlap_f1 config). Tasks scored before scorer_family
        # was wired contribute "" / unknown which we map to ``unspecified``.
        family_counts: Counter[str] = Counter()
        for r in cfg_rows:
            details = r.get("scoring_details") or {}
            fam = details.get("scorer_family")
            if isinstance(fam, str) and fam:
                family_counts[fam] += 1
            else:
                family_counts["unspecified"] += 1

        # ``mean_automated_score`` is the headline reward (recall-based for
        # IR scorers post-codeprobe-voxa). ``mean_reward`` is an alias for
        # callers who want an unambiguous name. ``ir_diagnostics`` carries
        # the F1 / precision / recall *measurements* (still computed, just
        # demoted from the headline so over-shipping doesn't fake a
        # capability-quality gap). See docs/scoring_model.md.
        mean_score = statistics.mean(scores) if scores else None
        mean_p = statistics.mean(precisions) if precisions else None
        mean_r = statistics.mean(recalls) if recalls else None
        mean_f = statistics.mean(f1s) if f1s else None
        config_summaries[cfg_label] = {
            "tasks_completed": len(cfg_rows),
            "quota_error_count": quota_error_count,
            "infra_failure_count": infra_failure_count,
            "errored_count": errored_count,
            "mean_automated_score": mean_score,
            "mean_reward": mean_score,
            "stdev_automated_score": (
                statistics.stdev(scores) if len(scores) > 1 else None
            ),
            "total_cost_usd": sum(costs) if costs else None,
            "mean_cost_per_task": (statistics.mean(costs) if costs else None),
            "total_time_seconds": sum(times) if times else None,
            # Raw token counts (codeprobe-oktg). Sum/mean over tasks that
            # reported usage; None when no task in the config did so.
            "total_input_tokens": (
                sum(input_tokens_per_task) if input_tokens_per_task else None
            ),
            "total_output_tokens": (
                sum(output_tokens_per_task) if output_tokens_per_task else None
            ),
            "mean_input_tokens_per_task": (
                statistics.mean(input_tokens_per_task)
                if input_tokens_per_task
                else None
            ),
            "mean_output_tokens_per_task": (
                statistics.mean(output_tokens_per_task)
                if output_tokens_per_task
                else None
            ),
            "total_cache_read_tokens": (
                sum(cache_read_per_task) if cache_read_per_task else None
            ),
            "total_cache_creation_tokens": (
                sum(cache_creation_per_task) if cache_creation_per_task else None
            ),
            "mean_cache_read_tokens_per_task": (
                statistics.mean(cache_read_per_task)
                if cache_read_per_task
                else None
            ),
            "mean_cache_creation_tokens_per_task": (
                statistics.mean(cache_creation_per_task)
                if cache_creation_per_task
                else None
            ),
            "score_per_dollar": (
                statistics.mean(scores) / statistics.mean(costs)
                if scores and costs and statistics.mean(costs) > 0
                else None
            ),
            # Back-compat: kept at the top level so older aggregate
            # consumers don't break. New code should read ir_diagnostics.
            "mean_precision": mean_p,
            "mean_recall": mean_r,
            "mean_f1": mean_f,
            "ir_diagnostics": {
                "mean_precision": mean_p,
                "mean_recall": mean_r,
                "mean_f1": mean_f,
            },
            # codeprobe-voxa (revised): which rubric produced each
            # task's reward. Sorted alphabetically so JSON diffs stay
            # stable across runs.
            "scorer_family_distribution": dict(sorted(family_counts.items())),
        }

    # Pairwise deltas
    config_labels = [c.label for c in experiment.configs]
    pairwise: list[dict] = []
    for i, a_label in enumerate(config_labels):
        for b_label in config_labels[i + 1 :]:
            # Accumulate every scored repeat per task; the per-task mean is
            # the statistical unit for --repeats (locked decision 6, epic
            # codeprobe-f7rl / codeprobe-f7rl.7).
            a_scores: dict[str, list[float]] = {}
            for r in config_results.get(a_label, []):
                if r["automated_score"] is not None:
                    a_scores.setdefault(r["task_id"], []).append(
                        r["automated_score"]
                    )
            b_scores: dict[str, list[float]] = {}
            for r in config_results.get(b_label, []):
                if r["automated_score"] is not None:
                    b_scores.setdefault(r["task_id"], []).append(
                        r["automated_score"]
                    )
            shared = set(a_scores) & set(b_scores)
            if not shared:
                # Zero shared tasks: the arms are incomparable. Emit an
                # explicit refusal entry instead of silently omitting the
                # pair (decision 6's refusal contract).
                pairwise.append(
                    {
                        "config_a": a_label,
                        "config_b": b_label,
                        "shared_tasks": 0,
                        "comparable": False,
                    }
                )
                continue

            deltas = [
                statistics.mean(b_scores[t]) - statistics.mean(a_scores[t])
                for t in shared
            ]
            mean_delta = statistics.mean(deltas)
            wins_b = sum(1 for d in deltas if d > 0.01)
            wins_a = sum(1 for d in deltas if d < -0.01)
            ties = len(deltas) - wins_b - wins_a

            cohens_d: float | None = None
            if len(deltas) > 1:
                sd = statistics.stdev(deltas)
                cohens_d = mean_delta / sd if sd > 0 else None

            pairwise.append(
                {
                    "config_a": a_label,
                    "config_b": b_label,
                    "shared_tasks": len(shared),
                    "mean_delta": round(mean_delta, 4),
                    "wins_a": wins_a,
                    "wins_b": wins_b,
                    "ties": ties,
                    "cohens_d": (
                        round(cohens_d, 3) if cohens_d is not None else None
                    ),
                }
            )

    # Bias detection — flag tautology and capability-boundary patterns.
    # Always computed so aggregate.json stays consistent across runs;
    # suppressed from stdout when --no-warn is set.
    from codeprobe.core.bias_detection import detect_bias_warnings

    bias_warnings, _ = detect_bias_warnings(experiment, exp_dir, config_results)
    suppress_winner = any(
        w.kind == "no_independent_baseline" for w in bias_warnings
    ) and not no_warn

    # Per-trial quality view derived from the typed CompletedTask records
    # (status, error_category, scoring_details) plus bias warnings. ZFC:
    # mechanical projection only, no semantic judgment.
    from codeprobe.analysis.trace_quality import TraceQualityReporter

    quality_reporter = TraceQualityReporter.from_completed_tasks(
        completed_by_config,
        bias_warnings=bias_warnings,
    )

    aggregate = {
        "experiment": experiment.name,
        "generated": _now_iso(),
        "config_count": len(experiment.configs),
        "config_summaries": config_summaries,
        "pairwise_deltas": pairwise,
        "bias_warnings": [w.to_dict() for w in bias_warnings],
        "quality_metrics": quality_reporter.to_dict(),
    }

    reports_dir = exp_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    out_path = reports_dir / "aggregate.json"
    out_path.write_text(json.dumps(aggregate, indent=2) + "\n", encoding="utf-8")

    if emit_json:
        # Embed the exact payload written to reports/aggregate.json so
        # stdout carries it — agents don't need a second read round-trip.
        # bias_warnings ride inside the payload, so nothing is lost by
        # skipping the pretty warning panel.
        emit_envelope(
            command="experiment aggregate",
            data={"aggregate": aggregate, "report_path": str(out_path)},
        )
        return

    # Print bias warnings prominently before the table so users see the
    # caveat in the same eyepath as the numbers. Severity-split (codeprobe-
    # 9re9): real tautology risks under "Bias warnings:"; signals that the
    # curator independently corroborated under "Informational:" so the
    # warnings panel only highlights actionable measurement bias.
    if bias_warnings and not no_warn:
        actionable = [
            w for w in bias_warnings
            if getattr(w, "severity", "warning") == "warning"
        ]
        informational = [
            w for w in bias_warnings
            if getattr(w, "severity", "warning") == "informational"
        ]
        if actionable:
            click.echo("Bias warnings:")
            for w in actionable:
                click.echo(f"  [{w.kind}] {w.message}")
            click.echo()
        if informational:
            click.echo("Informational:")
            for w in informational:
                click.echo(f"  [{w.kind}] {w.message}")
            click.echo()

    # Print summary table. Only render P/R columns if at least one config
    # exposed them — keeps the table compact for non-oracle experiments.
    has_pr = any(
        s.get("mean_precision") is not None or s.get("mean_recall") is not None
        for s in config_summaries.values()
    )
    click.echo(f"Experiment: {experiment.name}")
    if has_pr:
        click.echo(
            f"\n{'Configuration':<25} {'Score (auto)':<14} "
            f"{'Precision':<11} {'Recall':<8} "
            f"{'Cost/Task':<12} {'Score/$':<10}"
        )
        click.echo(
            f"{'-' * 25} {'-' * 14} "
            f"{'-' * 11} {'-' * 8} "
            f"{'-' * 12} {'-' * 10}"
        )
    else:
        click.echo(
            f"\n{'Configuration':<25} {'Score (auto)':<14} "
            f"{'Cost/Task':<12} {'Score/$':<10}"
        )
        click.echo(f"{'-' * 25} {'-' * 14} {'-' * 12} {'-' * 10}")

    if suppress_winner:
        # No independent baseline — show breakdown in declaration order
        # so readers can't infer a ranking from row position.
        ranked = list(config_summaries.items())
    else:
        ranked = sorted(
            config_summaries.items(),
            key=lambda x: x[1].get("mean_automated_score") or 0,
            reverse=True,
        )
    for label, s in ranked:
        auto = (
            f"{s['mean_automated_score']:.2f}"
            if s["mean_automated_score"] is not None
            else "--"
        )
        cost = (
            f"${s['mean_cost_per_task']:.2f}"
            if s["mean_cost_per_task"] is not None
            else "--"
        )
        spd = (
            f"{s['score_per_dollar']:.2f}"
            if s["score_per_dollar"] is not None
            else "--"
        )
        if has_pr:
            prec = (
                f"{s['mean_precision']:.2f}"
                if s.get("mean_precision") is not None
                else "--"
            )
            rec = (
                f"{s['mean_recall']:.2f}"
                if s.get("mean_recall") is not None
                else "--"
            )
            click.echo(
                f"{label:<25} {auto:<14} "
                f"{prec:<11} {rec:<8} "
                f"{cost:<12} {spd:<10}"
            )
        else:
            click.echo(f"{label:<25} {auto:<14} {cost:<12} {spd:<10}")

    if pairwise and not suppress_winner:
        click.echo("\nPairwise Comparisons:")
        for p in pairwise:
            if p.get("comparable") is False:
                click.echo(
                    f"  {p['config_a']} vs {p['config_b']}: "
                    "not comparable (no shared tasks)"
                )
                continue
            click.echo(
                f"  {p['config_a']} vs {p['config_b']}: "
                f"delta={p['mean_delta']:+.3f}  "
                f"(wins: {p['wins_a']}/{p['wins_b']}/{p['ties']} A/B/tie)  "
                f"d={p['cohens_d'] or '--'}"
            )
    elif pairwise and suppress_winner:
        click.echo(
            "\nPairwise comparisons suppressed: see bias_warnings in aggregate.json."
        )

    click.echo(f"\nFull results: {out_path}")
