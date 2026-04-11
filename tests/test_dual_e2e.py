"""u11: end-to-end integration test for the dual-verification pipeline.

This test exercises the FULL dual pipeline in a single flow:

    DualScorer (real)
        → executor.execute_task()
            → CompletedTask.scoring_details
                → CheckpointStore.append() / load_entries()
                    → analysis.summarize_completed_tasks()
                        → analysis.generate_report()
                            → format_text_report
                            → format_html_report
                            → format_csv_report
                            → format_json_report

The test FAILS LOUDLY if any rendered output format drops the artifact
dimension (score_artifact / artifact pass rate / "Artifact" column).

Layer 1 foundations relied on:

- ``codeprobe.core.scoring.DualScorer`` registered as ``"dual"``
- ``codeprobe.core.executor.execute_task`` with ``verification_mode='dual'``
  override and per-run scoring sandbox
- ``codeprobe.mining.writer`` dual task layout
  (``tests/test.sh`` + ``tests/ground_truth.json``)
- ``codeprobe.analysis.stats`` artifact pass rate / dual task count
- ``codeprobe.analysis.report`` artifact columns in all four formats
- ``CompletedTask.scoring_details`` round-tripping through checkpoint save/load
"""

from __future__ import annotations

import csv
import io
import json
import stat
import subprocess
from pathlib import Path

import pytest

from codeprobe.adapters.protocol import AgentConfig, AgentOutput
from codeprobe.analysis import (
    format_csv_report,
    format_html_report,
    format_json_report,
    format_text_report,
    generate_report,
    summarize_config,
)
from codeprobe.core.checkpoint import CheckpointStore
from codeprobe.core.executor import execute_task
from codeprobe.models.experiment import CompletedTask, ConfigResults

# ---------------------------------------------------------------------------
# Stub adapter — writes a known answer.json into the worktree
# ---------------------------------------------------------------------------


class _AnswerWritingAdapter:
    """Fake adapter that writes a known ``answer.json`` into the worktree.

    Mirrors the contract a real coding agent would satisfy for a dual task:
    it must produce both code changes (which our trivial test.sh accepts) and
    an artifact (``answer.json``) matching the oracle.
    """

    name = "answer-writing-adapter"

    def __init__(self, worktree: Path, answer_payload: dict) -> None:
        self._worktree = Path(worktree)
        self._payload = answer_payload
        self.run_calls = 0

    def find_binary(self) -> str | None:
        return "/usr/bin/true"

    def preflight(self, config: AgentConfig) -> list[str]:
        return []

    def build_command(self, prompt: str, config: AgentConfig) -> list[str]:
        return ["true"]

    def run(
        self,
        prompt: str,
        config: AgentConfig,
        session_env: dict[str, str] | None = None,
    ) -> AgentOutput:
        self.run_calls += 1
        # Write the artifact the executor will then copy into the scoring
        # sandbox before invoking the artifact leg of DualScorer.
        (self._worktree / "answer.json").write_text(
            json.dumps(self._payload), encoding="utf-8"
        )
        return AgentOutput(
            stdout="ok",
            stderr=None,
            exit_code=0,
            duration_seconds=0.42,
            cost_usd=0.001,
            cost_model="per_token",
        )

    def isolate_session(self, slot_id: int) -> dict[str, str]:
        return {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_git_repo(repo: Path) -> None:
    """Initialize a minimal git repo with one commit so worktrees work."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("seed\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", "README.md"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "init"],
        check=True,
        capture_output=True,
    )


def _make_dual_task(task_dir: Path, ground_truth_answer: list[str]) -> Path:
    """Create a minimal dual task fixture mirroring writer.py's layout.

    Layout::

        task_dir/
            instruction.md
            metadata.json            (verification_mode='dual')
            tests/test.sh            (exit 0 — direct leg passes)
            tests/ground_truth.json  (file_list oracle)
    """
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "instruction.md").write_text(
        "Make the change AND write answer.json with the file list.\n",
        encoding="utf-8",
    )

    tests = task_dir / "tests"
    tests.mkdir(parents=True, exist_ok=True)

    test_sh = tests / "test.sh"
    test_sh.write_text("#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n")
    test_sh.chmod(test_sh.stat().st_mode | stat.S_IEXEC)

    ground_truth = {
        "schema_version": 1,
        "answer_type": "file_list",
        "answer": ground_truth_answer,
    }
    (tests / "ground_truth.json").write_text(
        json.dumps(ground_truth, indent=2) + "\n", encoding="utf-8"
    )

    metadata = {
        "verification": {
            "verification_mode": "dual",
            "reward_type": "binary",
            "scoring_policy": "weighted",
            "weight_direct": 0.5,
            "weight_artifact": 0.5,
        }
    }
    (task_dir / "metadata.json").write_text(json.dumps(metadata) + "\n")

    return task_dir


def _csv_reader_skipping_comments(text: str) -> csv.DictReader:
    """Skip leading ``# ...`` header lines and parse the rest as CSV."""
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    return csv.DictReader(io.StringIO("\n".join(lines)))


# ---------------------------------------------------------------------------
# The end-to-end test
# ---------------------------------------------------------------------------


def test_dual_pipeline_end_to_end_preserves_artifact_dimension(
    tmp_path: Path,
) -> None:
    """Full dual pipeline E2E: every output format must keep artifact data.

    Acceptance criteria (u11):
      1. Real ``DualScorer`` runs both legs (direct test.sh + artifact F1)
      2. ``execute_task`` returns a ``CompletedTask`` with ``scoring_details``
         carrying ``score_direct`` and ``score_artifact``
      3. Checkpoint save → load round-trips ``scoring_details``
      4. ``summarize_config`` reports ``dual_task_count`` and
         ``artifact_pass_rate``
      5. All four report formats (text, HTML, CSV, JSON) include the
         artifact dimension
    """
    # ------------------------------------------------------------------
    # 1. Build a real git repo + a real dual task fixture
    # ------------------------------------------------------------------
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    expected_files = ["src/foo.py", "src/bar.py"]
    task = _make_dual_task(tmp_path / "task-dual-e2e", expected_files)

    # Caller-supplied worktree so the adapter knows exactly where to write
    # answer.json. Providing worktree_path ALSO bypasses the executor's
    # owned dual-isolation pool (verified by u6 tests).
    worktree = tmp_path / "worktree"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree)],
        check=True,
        capture_output=True,
    )

    adapter = _AnswerWritingAdapter(
        worktree=worktree,
        answer_payload={
            "answer_type": "file_list",
            "answer": expected_files,
        },
    )

    # ------------------------------------------------------------------
    # 2. Execute the task through the REAL executor + REAL DualScorer
    # ------------------------------------------------------------------
    result = execute_task(
        adapter=adapter,
        task_dir=task,
        repo_path=repo,
        agent_config=AgentConfig(),
        # Pass binary; the verification_mode='dual' override flips this to
        # 'dual' inside execute_task.
        reward_type="binary",
        worktree_path=worktree,
    )

    completed = result.completed
    assert completed.status == "completed", (
        f"execute_task returned status={completed.status!r} "
        f"with metadata={completed.metadata!r}; expected 'completed'. "
        f"This indicates the executor → DualScorer integration is broken."
    )
    assert adapter.run_calls == 1, "adapter.run was not invoked exactly once"

    sd = completed.scoring_details
    assert isinstance(sd, dict), "scoring_details must be a dict"
    assert "score_direct" in sd, (
        f"scoring_details missing 'score_direct' — DualScorer leg outputs "
        f"are not propagating into CompletedTask. Got keys: {sorted(sd)}"
    )
    assert "score_artifact" in sd, (
        f"scoring_details missing 'score_artifact' — the artifact dimension "
        f"is being dropped at the executor boundary. Got keys: {sorted(sd)}"
    )
    # With matching answer.json the artifact F1 should be 1.0
    assert sd["score_artifact"] == pytest.approx(1.0), (
        f"score_artifact={sd['score_artifact']} but expected 1.0 "
        f"(answer.json matches ground_truth.json exactly)"
    )
    assert sd.get("passed_artifact") is True

    # ------------------------------------------------------------------
    # 3. Persist the CompletedTask via the real CheckpointStore and
    #    reload it from disk so we exercise the serialization path.
    # ------------------------------------------------------------------
    db_path = tmp_path / "checkpoints.db"
    config_label = "dual-e2e-config"

    store = CheckpointStore(db_path, config_name=config_label)
    try:
        store.append(completed)
    finally:
        store.close()

    reloader = CheckpointStore(db_path, config_name=config_label)
    try:
        entries = reloader.load_entries()
    finally:
        reloader.close()

    assert len(entries) == 1, f"expected 1 checkpoint entry, got {len(entries)}"
    entry = entries[0]
    assert entry["task_id"] == completed.task_id
    assert "scoring_details" in entry, (
        "checkpoint entry dropped 'scoring_details' field — "
        "this would silently lose the artifact dimension on resume"
    )
    reloaded_sd = entry["scoring_details"]
    assert reloaded_sd.get("score_artifact") == pytest.approx(
        1.0
    ), f"checkpoint round-trip lost score_artifact: {reloaded_sd!r}"
    assert (
        reloaded_sd.get("score_direct") is not None
    ), f"checkpoint round-trip lost score_direct: {reloaded_sd!r}"

    # Reconstruct a CompletedTask from the loaded entry — exactly the
    # path execute_config takes when restoring from checkpoint.
    reloaded = CompletedTask(
        task_id=entry["task_id"],
        automated_score=entry["automated_score"],
        status=entry.get("status", "completed"),
        duration_seconds=entry.get("duration_seconds", 0.0),
        cost_usd=entry.get("cost_usd"),
        cost_model=entry.get("cost_model", "unknown"),
        scoring_details=reloaded_sd,
    )

    # ------------------------------------------------------------------
    # 4. Summary statistics: dual fields must be populated
    # ------------------------------------------------------------------
    config_results = ConfigResults(config=config_label, completed=[reloaded])
    summary = summarize_config(config_results)
    assert summary.dual_task_count == 1, (
        f"summarize_config saw dual_task_count={summary.dual_task_count}, "
        f"expected 1 — stats layer is dropping the dual dimension"
    )
    assert summary.artifact_pass_rate == pytest.approx(1.0), (
        f"summarize_config produced artifact_pass_rate="
        f"{summary.artifact_pass_rate}, expected 1.0"
    )
    assert summary.direct_pass_rate == pytest.approx(1.0), (
        f"summarize_config produced direct_pass_rate="
        f"{summary.direct_pass_rate}, expected 1.0"
    )

    # ------------------------------------------------------------------
    # 5. Render reports in ALL FOUR formats and assert each one
    #    surfaces the artifact dimension.
    # ------------------------------------------------------------------
    report = generate_report("dual-e2e-experiment", [config_results])

    # ---- text ----
    text = format_text_report(report)
    assert "Artifact" in text, (
        "TEXT report dropped the 'Artifact' column — "
        "format_text_report is missing the artifact dimension.\n"
        f"Report contents:\n{text}"
    )

    # ---- HTML ----
    html = format_html_report(report)
    assert "<th>Artifact</th>" in html, (
        "HTML report dropped the '<th>Artifact</th>' column — "
        "format_html_report is missing the artifact dimension."
    )

    # ---- CSV ----
    csv_text = format_csv_report(report)
    reader = _csv_reader_skipping_comments(csv_text)
    fieldnames = reader.fieldnames or []
    assert "score_artifact" in fieldnames, (
        f"CSV report dropped the 'score_artifact' column. "
        f"Got fieldnames: {fieldnames}"
    )
    assert "passed_artifact" in fieldnames, (
        f"CSV report dropped the 'passed_artifact' column. "
        f"Got fieldnames: {fieldnames}"
    )
    csv_rows = list(reader)
    dual_rows = [r for r in csv_rows if r.get("config") == config_label]
    assert dual_rows, "CSV report has no rows for the dual config"
    assert float(dual_rows[0]["score_artifact"]) == pytest.approx(1.0), (
        f"CSV row score_artifact={dual_rows[0]['score_artifact']!r}, " f"expected 1.0"
    )

    # ---- JSON ----
    json_text = format_json_report(report)
    data = json.loads(json_text)
    summaries_by_label = {s["label"]: s for s in data["summaries"]}
    assert (
        config_label in summaries_by_label
    ), f"JSON report missing config {config_label!r} in summaries"
    json_summary = summaries_by_label[config_label]
    assert json_summary.get("artifact_pass_rate") is not None, (
        "JSON report summary dropped 'artifact_pass_rate' "
        "(value is None for a dual task)"
    )
    assert json_summary["artifact_pass_rate"] == pytest.approx(1.0)
    assert json_summary.get("dual_task_count") == 1, (
        f"JSON summary dual_task_count="
        f"{json_summary.get('dual_task_count')}, expected 1"
    )

    json_tasks = [t for t in data["tasks"] if t.get("config") == config_label]
    assert json_tasks, "JSON report has no tasks for the dual config"
    json_sd = json_tasks[0].get("scoring_details") or {}
    assert json_sd.get("score_artifact") == pytest.approx(
        1.0
    ), f"JSON task scoring_details dropped 'score_artifact': {json_sd!r}"
