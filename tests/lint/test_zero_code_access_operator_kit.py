"""Contract and clean-environment checks for the zero-code-access operator kit."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from codeprobe import __version__
from codeprobe.cli import main
from codeprobe.snapshot.evidence_bundle import (
    ARTIFACT_FILENAMES,
    load_evidence_request,
    preview_evidence_bundle,
)
from codeprobe.snapshot.evidence_models import SupportEvent

REPO_ROOT = Path(__file__).resolve().parents[2]
KIT_ROOT = REPO_ROOT / "docs" / "pilot" / "zero-code-access"
TEMPLATES = KIT_ROOT / "templates"
REQUIRED_FILES = (
    KIT_ROOT / "README.md",
    KIT_ROOT / "kit-contract.json",
    KIT_ROOT / "participant-runbook.md",
    KIT_ROOT / "se-methodology.md",
    KIT_ROOT / "fe-coordination.md",
    TEMPLATES / "intake-and-consent.md",
    TEMPLATES / "sampling-plan.md",
    TEMPLATES / "bounded-findings.md",
    TEMPLATES / "intervention-log.json",
    TEMPLATES / "experiment.template.json",
    TEMPLATES / "evidence-request.template.json",
)
LOCAL_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
QA_VALID_EXAMPLES = (
    "comprehension/count-classes",
    "comprehension/count-functions",
    "comprehension/count-test-files",
    "sdlc/add-docstring",
    "sdlc/add-logging",
    "sdlc/add-null-check",
    "sdlc/fix-import",
    "sdlc/fix-off-by-one",
    "sdlc/handle-edge-case",
)


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _digest(label: str) -> str:
    return f"sha256:{hashlib.sha256(label.encode()).hexdigest()}"


def _materialize_evidence_template(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _materialize_evidence_template(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_materialize_evidence_template(item) for item in value]
    if value == "__CODEPROBE_VERSION__":
        return __version__
    if value == "__START_DATE__":
        return "2026-01-01"
    if value == "__END_DATE__":
        return "2026-06-30"
    if value == "__NETWORK_POSTURE__":
        return "restricted"
    if isinstance(value, str) and value.startswith("__SHA256_"):
        return _digest(value)
    return value


def _documented_cli_paths(runbook: str) -> tuple[tuple[str, ...], ...]:
    paths: set[tuple[str, ...]] = set()
    for line in runbook.splitlines():
        if not line.startswith("codeprobe "):
            continue
        tokens = shlex.split(line.removesuffix("\\").strip())
        command: Any = main
        path: list[str] = []
        for token in tokens[1:]:
            children = getattr(command, "commands", {})
            if token not in children:
                break
            path.append(token)
            command = children[token]
        if path:
            paths.add(tuple(path))
    return tuple(sorted(paths))


def _commit_clean_fixture(repo: Path) -> None:
    (repo / "README.md").write_text("# Participant fixture\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=CodeProbe",
            "-c",
            "user.email=codeprobe@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=repo,
        check=True,
    )


def _write_promoted_confidence(task_dir: Path) -> None:
    task_id = task_dir.name
    (task_dir / "confidence.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "score": 0.85,
                "threshold": 0.5,
                "breakdown": {},
                "notes": {},
                "promoted": True,
            }
        ),
        encoding="utf-8",
    )


def _materialize_qa_valid_tasks(tasks_dir: Path) -> None:
    examples = REPO_ROOT / "examples" / "dual"
    for relative in QA_VALID_EXAMPLES:
        source = examples / relative
        destination = tasks_dir / source.name
        shutil.copytree(source, destination)
        _write_promoted_confidence(destination)
    source = REPO_ROOT / "tests" / "fixtures" / "dual_task"
    destination = tasks_dir / "dual-task-001"
    shutil.copytree(source, destination)
    (destination / "tests" / "test.sh").chmod(0o755)
    (destination / "tests" / "ground_truth.json").write_text(
        json.dumps({"answer_type": "boolean", "answer": True}),
        encoding="utf-8",
    )
    _write_promoted_confidence(destination)


def _materialize_stub_profile(experiment_dir: Path) -> None:
    profile = _json(TEMPLATES / "experiment.template.json")
    for config in profile["configs"]:
        config["agent"] = "e2e-stub"
        config["model"] = None
    (experiment_dir / "experiment.json").write_text(
        json.dumps(profile),
        encoding="utf-8",
    )


def _assert_validated_sixty_run_plan(runner: CliRunner) -> None:
    task_validation = runner.invoke(
        main,
        ["validate", ".codeprobe/tasks", "--qa", "--no-json"],
    )
    assert task_validation.exit_code == 0, task_validation.output
    assert "Validated 10 task(s): 10 passed, 0 failed." in task_validation.output
    validation = runner.invoke(
        main,
        ["experiment", "validate", ".codeprobe", "--no-json"],
    )
    assert validation.exit_code == 0, validation.output
    assert "Tasks: 10" in validation.output
    dry_run = runner.invoke(
        main,
        [
            "run",
            ".",
            "--config",
            ".codeprobe",
            "--repeats",
            "3",
            "--dry-run",
            "--no-json",
        ],
        env={"CODEPROBE_SANDBOX": "1"},
    )
    assert dry_run.exit_code == 0, dry_run.output
    assert "Total runs:             60" in dry_run.output


def test_operator_kit_has_every_required_file_and_resolvable_link() -> None:
    assert all(path.is_file() for path in REQUIRED_FILES)

    for document in KIT_ROOT.rglob("*.md"):
        for target in LOCAL_LINK.findall(document.read_text(encoding="utf-8")):
            target_path = target.split("#", 1)[0]
            if not target_path or "://" in target_path or target_path.startswith("mailto:"):
                continue
            assert (document.parent / target_path).resolve().exists(), (
                f"{document.relative_to(REPO_ROOT)} links to missing {target_path}"
            )


def test_kit_contract_preserves_pilot_thresholds_and_boundary() -> None:
    contract = _json(KIT_ROOT / "kit-contract.json")

    assert contract["schema_version"] == "codeprobe.zero-code-access.operator-kit.v1"
    assert contract["participant_time_budget_minutes"] == {
        "asynchronous_intake_maximum": 10,
        "structured_session_maximum": 45,
    }
    assert contract["security_or_platform_followup"] == "optional"
    assert contract["minimum_paired_distinct_tasks"] == 10
    assert contract["minimum_repeats_per_task_and_configuration"] == 3
    assert contract["same_task_set_required"] is True
    assert contract["external_execution_owner"] == "participant_technical_owner"
    assert contract["sourcegraph_repository_access"] == "prohibited"
    assert contract["allowed_conclusions"] == [
        "advance_a",
        "advance_b",
        "insufficient_evidence",
    ]
    assert contract["prohibited_outbound_data"] == [
        "source",
        "repository_identifiers",
        "paths",
        "prompts",
        "patches",
        "traces",
        "task_level_results",
        "raw_results",
        "logs",
        "diagnostics",
    ]


def test_standard_profile_declares_exactly_two_symmetric_arms() -> None:
    profile = _json(TEMPLATES / "experiment.template.json")
    configs = profile["configs"]

    assert profile["name"] == "zero-code-access-pilot"
    assert profile["tasks_dir"] == "tasks"
    assert [item["label"] for item in configs] == ["A", "B"]
    assert len(configs) == 2
    for config in configs:
        assert config["max_turns"] is None
        assert config["extra"]["pilot_protocol"] == "CP-ZCA-PILOT-2026"
        assert config["extra"]["minimum_paired_tasks"] == 10
        assert config["extra"]["required_repeats"] == 3
    symmetric_fields = {
        "permission_mode",
        "mcp_mode",
        "reward_type",
        "max_turns",
        "hide_local_source",
        "low_confidence_threshold",
    }
    assert all(configs[0][field] == configs[1][field] for field in symmetric_fields)


def test_evidence_request_template_previews_after_local_values_are_filled(
    tmp_path: Path,
) -> None:
    template = _json(TEMPLATES / "evidence-request.template.json")
    assert template["run"]["environment"]["network_posture"] == (
        "__NETWORK_POSTURE__"
    )
    materialized = _materialize_evidence_template(template)
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(materialized), encoding="utf-8")

    request = load_evidence_request(request_path)
    preview = preview_evidence_bundle(request)

    assert request.results.paired_task_count == 10
    assert request.results.repeats_per_task == 3
    assert request.finding.conclusion == "insufficient_evidence"
    assert tuple(artifact.filename for artifact in preview.artifacts) == (
        ARTIFACT_FILENAMES
    )


def test_intervention_log_examples_match_runtime_disqualification_policy() -> None:
    template = _json(TEMPLATES / "intervention-log.json")

    for expected, key in (
        (False, "permitted_examples"),
        (True, "disqualifying_examples"),
    ):
        events = template[key]
        assert events
        assert all(
            SupportEvent(
                sequence=index,
                actor_role=event["actor_role"],
                kind=event["kind"],
            ).disqualifying
            is expected
            for index, event in enumerate(events, 1)
        )


def test_participant_runbook_names_only_resolvable_cli_surfaces() -> None:
    runbook = (KIT_ROOT / "participant-runbook.md").read_text(encoding="utf-8")
    required = {
        ("doctor",),
        ("mine",),
        ("validate",),
        ("experiment", "init"),
        ("experiment", "validate"),
        ("experiment", "status"),
        ("run",),
        ("experiment", "aggregate"),
        ("snapshot", "evidence", "preview"),
        ("snapshot", "evidence", "export"),
    }
    commands = _documented_cli_paths(runbook)
    runner = CliRunner()

    assert runbook.index('python -m pip install "codeprobe==$CODEPROBE_VERSION"') < (
        runbook.index("codeprobe experiment init")
    )
    assert 'python3 -m venv "$CODEPROBE_VENV"' in runbook
    assert ".codeprobe-venv" not in runbook
    assert required <= set(commands)
    for command in commands:
        result = runner.invoke(main, [*command, "--help"])
        assert result.exit_code == 0, result.output


def test_standard_profile_dry_runs_sixty_trials_from_clean_repository(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    repo = tmp_path / "participant-repo"
    experiment_dir = repo / ".codeprobe"
    repo.mkdir()
    _commit_clean_fixture(repo)
    monkeypatch.chdir(repo)
    runner = CliRunner()
    initialized = runner.invoke(
        main,
        ["experiment", "init", ".", "--non-interactive", "--no-json"],
    )
    assert initialized.exit_code == 0, initialized.output
    _materialize_stub_profile(experiment_dir)
    _materialize_qa_valid_tasks(experiment_dir / "tasks")
    external_venv = tmp_path / "participant-tools" / "codeprobe-venv"
    external_venv.mkdir(parents=True)
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == ""
    _assert_validated_sixty_run_plan(runner)
