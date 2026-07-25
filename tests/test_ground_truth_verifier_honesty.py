"""Tests for codeprobe-sh8c: a malformed oracle is a verifier fault.

A ground_truth.json the harness cannot score is the harness's problem, not
the agent's. Before this, an unscoreable oracle produced an ordinary 0.0
agent loss with ``verdict`` unset:

- ``{}`` has neither 'checks' nor 'answer_type', so it slipped past both
  dispatch guards into ``_score_legacy_format`` and returned 0.0.
- An ``answer_type`` the scorer cannot dispatch (``integer``, ``string``,
  ``list`` — used by 15 of the 20 shipped example fixtures) returned 0.0
  even when the agent submitted the exactly correct answer.
- ``DualScorer`` blended that fabricated 0.0 into its composite, so the
  agent still lost half its score for a broken oracle.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
from pathlib import Path

import pytest

from codeprobe.core.registry import available_oracle_scorers
from codeprobe.core.scoring import ScoreResult
from codeprobe.core.scoring.scorers import (
    ArtifactScorer,
    DualScorer,
    load_ground_truth,
    validate_ground_truth,
)


def _task_with(tmp_path: Path, gt: object, answer: object) -> Path:
    """Build a task dir carrying *gt* as the oracle and *answer* as output."""
    task_dir = tmp_path / "task"
    (task_dir / "tests").mkdir(parents=True, exist_ok=True)
    (task_dir / "tests" / "ground_truth.json").write_text(json.dumps(gt))
    (task_dir / "answer.json").write_text(json.dumps(answer))
    return task_dir


def _register_unloadable_oracle(
    monkeypatch: pytest.MonkeyPatch,
    answer_type: str,
) -> None:
    """Expose a registry name whose entry point cannot actually load."""
    from codeprobe.core import registry

    def _unloadable(name: str) -> object:
        assert name == answer_type
        raise KeyError(f"oracle scorer {name!r} could not be loaded")

    monkeypatch.setattr(registry, "resolve_oracle_scorer", _unloadable)


class TestValidatorRejectsUnscoreableOracles:
    """The canonical validator must reject what the scorer cannot dispatch."""

    def test_rejects_unknown_answer_type(self) -> None:
        """An answer_type with no scorer is oracle breakage, not an answer."""
        err = validate_ground_truth({"answer_type": "bogus_type", "answer": "x"})
        assert err is not None
        assert "bogus_type" in err

    @pytest.mark.parametrize("bad", [None, 5, ["file_list"], {"a": 1}])
    def test_rejects_non_string_answer_type(self, bad: object) -> None:
        """answer_type is used as a dict key — a non-str can never dispatch."""
        assert validate_ground_truth({"answer_type": bad, "answer": "x"}) is not None

    def test_accepts_every_registered_answer_type(self) -> None:
        """Validation is driven by the registry, not a hardcoded allowlist.

        Gating on a frozenset would reject entry-point plugin answer types
        as verifier errors — the plugin author's working task reported as
        harness breakage.
        """
        sample = {
            "file_list": ["a.py"],
            "symbol_list": ["f"],
            "dependency_chain": ["a"],
            "count": 1,
            "boolean": True,
            "text": "x",
        }
        for answer_type in available_oracle_scorers():
            answer = sample.get(answer_type, "x")
            gt = {"answer_type": answer_type, "answer": answer}
            assert validate_ground_truth(gt) is None, f"{answer_type} rejected"

    def test_rejects_empty_answer_list(self) -> None:
        """An empty expected set scores every answer 0.0 — unscoreable.

        scaffold/writer.py already refuses to *generate* this; generation
        and scoring must agree on what counts as a usable oracle.
        """
        assert validate_ground_truth({"answer_type": "file_list", "answer": []}) is not None

    def test_rejects_empty_legacy_expected(self) -> None:
        assert validate_ground_truth({"expected": []}) is not None

    def test_still_accepts_valid_oracles(self) -> None:
        """Valid legacy/v1/v2 fixtures keep validating (explicit AC)."""
        assert validate_ground_truth({"expected": ["a.py"]}) is None
        assert validate_ground_truth({"answer_type": "count", "answer": 5}) is None
        assert (
            validate_ground_truth(
                {"checks": [{"answer_type": "count", "answer": 1, "weight": 1.0}]}
            )
            is None
        )

    @pytest.mark.parametrize(
        ("oracle_format", "answer_type", "expected"),
        [
            ("v2", "file_list", "a.py"),
            ("v2", "file_list", []),
            ("v2", "count", [1]),
            ("v1", "count", True),
            ("v2", "count", True),
            ("v1", "count", 1.9),
            ("v2", "count", 1.9),
            ("v1", "file_list", [1]),
            ("v2", "file_list", [1]),
            ("v1", "file_list", ["  "]),
            ("v2", "file_list", ["  "]),
            ("v1", "symbol_list", [1]),
            ("v2", "symbol_list", [1]),
            ("v1", "symbol_list", ["\t"]),
            ("v2", "symbol_list", ["\t"]),
            ("v1", "dependency_chain", [1]),
            ("v2", "dependency_chain", [1]),
            ("v1", "dependency_chain", ["\n"]),
            ("v2", "dependency_chain", ["\n"]),
        ],
        ids=[
            "v2-file-list-string",
            "v2-file-list-empty",
            "v2-count-list",
            "v1-count-bool",
            "v2-count-bool",
            "v1-count-fractional",
            "v2-count-fractional",
            "v1-file-list-non-string",
            "v2-file-list-non-string",
            "v1-file-list-whitespace",
            "v2-file-list-whitespace",
            "v1-symbol-list-non-string",
            "v2-symbol-list-non-string",
            "v1-symbol-list-whitespace",
            "v2-symbol-list-whitespace",
            "v1-dependency-chain-non-string",
            "v2-dependency-chain-non-string",
            "v1-dependency-chain-whitespace",
            "v2-dependency-chain-whitespace",
        ],
    )
    def test_rejects_invalid_builtin_answer_shape_as_verifier_error(
        self,
        tmp_path: Path,
        oracle_format: str,
        answer_type: str,
        expected: object,
    ) -> None:
        if oracle_format == "v1":
            ground_truth = {"answer_type": answer_type, "answer": expected}
        else:
            ground_truth = {
                "checks": [
                    {
                        "answer_type": answer_type,
                        "answer": expected,
                        "weight": 1.0,
                    }
                ]
            }
        assert validate_ground_truth(ground_truth) is not None

        result = ArtifactScorer().score(
            "",
            _task_with(tmp_path, ground_truth, {"answers": []}),
        )

        assert result.verdict == "verifier_error"
        assert result.score == 0.0
        assert result.error is not None

    @pytest.mark.parametrize(
        "weight",
        [True, "1.0", 10**1000],
        ids=["bool", "string", "overflow"],
    )
    def test_rejects_invalid_v2_weight_as_verifier_error(
        self,
        tmp_path: Path,
        weight: object,
    ) -> None:
        ground_truth = {
            "checks": [
                {"answer_type": "count", "answer": 1, "weight": weight},
            ]
        }
        assert validate_ground_truth(ground_truth) is not None

        result = ArtifactScorer().score(
            "",
            _task_with(tmp_path, ground_truth, {"answers": []}),
        )

        assert result.verdict == "verifier_error"
        assert result.score == 0.0
        assert result.error is not None and "weight" in result.error

    @pytest.mark.parametrize(
        "expected",
        [[1], [""], ["  "]],
        ids=["non-string", "empty-string", "whitespace"],
    )
    def test_rejects_invalid_legacy_expected_as_verifier_error(
        self,
        tmp_path: Path,
        expected: list[object],
    ) -> None:
        ground_truth = {"expected": expected}
        assert validate_ground_truth(ground_truth) is not None

        result = ArtifactScorer().score(
            "",
            _task_with(tmp_path, ground_truth, {"answer": []}),
        )

        assert result.verdict == "verifier_error"
        assert result.score == 0.0
        assert result.error is not None

    @pytest.mark.parametrize("answer", [5, "5", " -5 "])
    def test_accepts_canonical_count_answers(self, answer: object) -> None:
        assert validate_ground_truth({"answer_type": "count", "answer": answer}) is None

    @pytest.mark.parametrize("oracle_format", ["v1", "v2"])
    @pytest.mark.parametrize(
        "confidence",
        ["high", True, float("nan"), -0.1, 1.1],
        ids=["string", "bool", "non-finite", "below-range", "above-range"],
    )
    def test_rejects_invalid_confidence_as_verifier_error(
        self,
        tmp_path: Path,
        oracle_format: str,
        confidence: object,
    ) -> None:
        if oracle_format == "v1":
            ground_truth = {
                "answer_type": "count",
                "answer": 1,
                "confidence": confidence,
            }
        else:
            ground_truth = {
                "checks": [
                    {"answer_type": "count", "answer": 1, "weight": 1.0}
                ],
                "confidence": confidence,
            }
        assert validate_ground_truth(ground_truth) is not None

        result = ArtifactScorer().score(
            "",
            _task_with(tmp_path, ground_truth, {"answer": 1}),
        )

        assert result.verdict == "verifier_error"
        assert result.score == 0.0
        assert result.error is not None and "confidence" in result.error

    @pytest.mark.parametrize("confidence", [0, 0.5, 1])
    def test_accepts_bounded_finite_confidence(self, confidence: float) -> None:
        assert (
            validate_ground_truth(
                {
                    "answer_type": "count",
                    "answer": 1,
                    "confidence": confidence,
                }
            )
            is None
        )


class TestArtifactScorerAttributesOracleFaultsToTheVerifier:
    def test_empty_ground_truth_is_a_verifier_error(self, tmp_path: Path) -> None:
        result = ArtifactScorer().score("", _task_with(tmp_path, {}, {"files": []}))
        assert result.verdict == "verifier_error"
        assert result.score == 0.0
        assert result.error

    def test_unknown_answer_type_is_a_verifier_error(self, tmp_path: Path) -> None:
        """The shipped-fixture bug: a CORRECT answer scored as an agent loss."""
        task_dir = _task_with(
            tmp_path, {"answer_type": "integer", "answer": 5}, {"answer": 5}
        )
        result = ArtifactScorer().score("", task_dir)
        assert result.verdict == "verifier_error", (
            "an oracle the scorer cannot dispatch must never be billed to "
            "the agent — this answer was exactly correct"
        )

    def test_valid_oracle_still_scores_the_agent(self, tmp_path: Path) -> None:
        """Validation must not swallow genuine agent grading."""
        task_dir = _task_with(
            tmp_path, {"answer_type": "count", "answer": 5}, {"answer": 5}
        )
        result = ArtifactScorer().score("", task_dir)
        assert result.verdict != "verifier_error"
        assert result.score == 1.0

    def test_low_confidence_warning_redacts_task_path(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        secret = "sk-" + "f" * 32
        task_dir = _task_with(
            tmp_path / secret,
            {"answer_type": "count", "answer": 1, "confidence": 0.1},
            {"answer": 1},
        )

        with caplog.at_level(logging.WARNING, logger="codeprobe.core.scoring"):
            result = ArtifactScorer().score("", task_dir)

        assert result.score == 1.0
        assert secret not in caplog.text
        assert "[REDACTED]" in caplog.text

    def test_registered_but_unloadable_type_is_a_verifier_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        answer_type = "broken_plugin_type"
        _register_unloadable_oracle(monkeypatch, answer_type)
        task_dir = _task_with(
            tmp_path,
            {"answer_type": answer_type, "answer": "expected"},
            {"answer": "actual"},
        )

        result = ArtifactScorer().score("", task_dir)

        assert result.verdict == "verifier_error"
        assert result.score == 0.0
        assert result.error is not None and "could not be loaded" in result.error

    def test_runtime_plugin_resolution_failure_is_a_verifier_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A plugin disappearing after validation is still a harness fault."""
        from codeprobe.core import registry

        answer_type = "sk-" + "b" * 32
        calls = 0

        def _resolve(name: str) -> object:
            nonlocal calls
            assert name == answer_type
            calls += 1
            if calls == 1:
                return lambda expected, actual: None
            raise KeyError(f"oracle scorer {name!r} disappeared")

        monkeypatch.setattr(registry, "resolve_oracle_scorer", _resolve)
        task_dir = _task_with(
            tmp_path,
            {"answer_type": answer_type, "answer": "expected"},
            {"answer": "actual"},
        )

        result = ArtifactScorer().score("", task_dir)

        assert result.verdict == "verifier_error"
        assert result.error is not None and "disappeared" in result.error
        assert answer_type not in result.error
        assert answer_type not in result.scorer_family

    def test_secret_shaped_answer_type_is_redacted(self, tmp_path: Path) -> None:
        secret = "sk-" + "a" * 32
        task_dir = _task_with(
            tmp_path,
            {"answer_type": secret, "answer": "expected"},
            {"answer": "actual"},
        )

        result = ArtifactScorer().score("", task_dir)

        assert result.verdict == "verifier_error"
        assert result.error is not None
        assert "unknown answer_type" in result.error
        assert secret not in result.error
        assert "[REDACTED]" in result.error

    @pytest.mark.parametrize("oracle_format", ["v1", "v2"])
    @pytest.mark.parametrize("plugin_failure", ["raises", "invalid_return"])
    def test_plugin_execution_failure_is_a_verifier_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        oracle_format: str,
        plugin_failure: str,
    ) -> None:
        """Plugin execution and return validation are verifier boundaries."""
        from codeprobe.core import registry

        answer_type = "execution_plugin"
        secret = "sk-" + "e" * 32

        def _plugin(expected: object, actual: object) -> object:
            if plugin_failure == "raises":
                raise RuntimeError(f"plugin exploded with {secret}")
            return None

        monkeypatch.setattr(
            registry,
            "resolve_oracle_scorer",
            lambda name: _plugin,
        )
        if oracle_format == "v1":
            ground_truth = {"answer_type": answer_type, "answer": "expected"}
            answer = {"answer": "actual"}
        else:
            ground_truth = {
                "checks": [
                    {
                        "answer_type": answer_type,
                        "answer": "expected",
                        "weight": 1.0,
                    }
                ]
            }
            answer = {
                "answers": [
                    {"answer_type": answer_type, "answer": "actual"},
                ]
            }
        task_dir = _task_with(tmp_path, ground_truth, answer)

        result = ArtifactScorer().score("", task_dir)

        assert result.verdict == "verifier_error"
        assert result.score == 0.0
        assert result.passed is False
        assert result.error is not None
        assert secret not in result.error
        if plugin_failure == "raises":
            assert "[REDACTED]" in result.error
            assert "RuntimeError" in result.error
        else:
            assert "ScoreResult" in result.error

    @pytest.mark.parametrize("oracle_format", ["v1", "v2"])
    def test_valid_plugin_score_is_preserved(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        oracle_format: str,
    ) -> None:
        from codeprobe.core import registry

        answer_type = "valid_plugin"

        def _plugin(expected: object, actual: object) -> ScoreResult:
            return ScoreResult(
                score=1.0,
                passed=True,
                scorer_family="exact_match",
            )

        monkeypatch.setattr(
            registry,
            "resolve_oracle_scorer",
            lambda name: _plugin,
        )
        if oracle_format == "v1":
            ground_truth = {"answer_type": answer_type, "answer": "expected"}
            answer = {"answer": "actual"}
        else:
            ground_truth = {
                "checks": [
                    {
                        "answer_type": answer_type,
                        "answer": "expected",
                        "weight": 1.0,
                    }
                ]
            }
            answer = {
                "answers": [
                    {"answer_type": answer_type, "answer": "actual"},
                ]
            }
        task_dir = _task_with(tmp_path, ground_truth, answer)

        result = ArtifactScorer().score("", task_dir)

        assert result.score == 1.0
        assert result.passed is True
        assert result.verdict is None


class TestDualScorerPropagatesVerifierErrors:
    """A verifier-errored leg must not be blended as a real 0.0."""

    def test_dual_does_not_blend_a_broken_oracle(self, tmp_path: Path) -> None:

        task_dir = _task_with(tmp_path, {}, {"files": []})
        (task_dir / "tests" / "test.sh").write_text("#!/bin/bash\nexit 0\n")
        (task_dir / "tests" / "test.sh").chmod(0o755)
        # The direct leg passes; only the oracle is broken. Without the fix
        # the composite would be (1.0 + 0.0)/2 = 0.5 — a real score cut.
        (task_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "verification": {
                        "verification_mode": "dual",
                        "scoring_policy": "mean",
                    }
                }
            )
        )

        result = DualScorer().score("", task_dir)
        assert result.verdict == "verifier_error", (
            "dual is the mode executor.py forces for every dual task; if it "
            "drops the artifact leg's verifier_error the agent still eats a "
            "score cut for a broken oracle"
        )

    def test_dual_does_not_blend_an_unloadable_plugin(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        answer_type = "broken_plugin_type"
        _register_unloadable_oracle(monkeypatch, answer_type)
        task_dir = _task_with(
            tmp_path,
            {"answer_type": answer_type, "answer": "expected"},
            {"answer": "actual"},
        )
        (task_dir / "tests" / "test.sh").write_text("#!/bin/bash\nexit 0\n")
        (task_dir / "tests" / "test.sh").chmod(0o755)
        (task_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "verification": {
                        "verification_mode": "dual",
                        "scoring_policy": "mean",
                    }
                }
            )
        )

        result = DualScorer().score("", task_dir)

        assert result.verdict == "verifier_error"
        assert result.score == 0.0
        assert result.details["verifier_error_leg"] == "artifact"


class TestOracleFileIsReadSafely:
    """ground_truth.json is untrusted input, read through a guarded fd."""

    def test_fifo_oracle_is_rejected_without_blocking(self, tmp_path: Path) -> None:
        """A FIFO at the oracle path must fail instead of hanging the run."""
        gt = tmp_path / "ground_truth.json"
        os.mkfifo(gt)

        result_queue: queue.Queue[tuple[dict | None, str | None]] = queue.Queue()
        worker = threading.Thread(
            target=lambda: result_queue.put(load_ground_truth(tmp_path)),
            daemon=True,
        )
        worker.start()
        worker.join(timeout=1.0)

        assert not worker.is_alive(), "load_ground_truth blocked while opening a FIFO"
        data, problem = result_queue.get_nowait()
        assert data is None
        assert problem is not None and "not a regular file" in problem

    def test_non_utf8_oracle_is_a_verifier_error_not_a_crash(
        self, tmp_path: Path
    ) -> None:
        """A non-UTF-8 oracle used to raise UnicodeDecodeError.

        UnicodeDecodeError subclasses ValueError, not JSONDecodeError, so it
        escaped the parse guard entirely: it crashed ArtifactScorer, and in
        DualScorer _safe_leg_score caught it and fabricated a 0.0 with no
        verdict — the agent billed for an unreadable oracle.
        """

        gt = tmp_path / "ground_truth.json"
        gt.write_bytes(b'{"answer_type": "count", "answer": \xff\xfe}')

        data, problem = load_ground_truth(tmp_path)
        assert data is None
        assert problem is not None and "invalid JSON" in problem

    def test_symlinked_oracle_is_refused(self, tmp_path: Path) -> None:
        """A symlinked oracle must not be read as its target.

        Path.is_file() follows symlinks, so this previously read an
        arbitrary local file under the size cap and parsed it as the oracle.
        """

        target = tmp_path / "elsewhere.json"
        target.write_text(json.dumps({"expected": ["leaked.py"]}))
        link = tmp_path / "ground_truth.json"
        link.symlink_to(target)

        data, problem = load_ground_truth(tmp_path)
        assert data is None, "a symlinked oracle was followed and read"
        assert problem is not None and "symlink" in problem

    def test_symlinked_tests_directory_is_refused(self, tmp_path: Path) -> None:
        """The guarded leaf must not be reachable through a symlinked parent."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "ground_truth.json").write_text(
            json.dumps({"answer_type": "count", "answer": 3})
        )
        task_dir = tmp_path / "task"
        task_dir.mkdir()
        (task_dir / "tests").symlink_to(outside, target_is_directory=True)
        (task_dir / "answer.json").write_text(json.dumps({"answer": 3}))

        result = ArtifactScorer().score("", task_dir)

        assert result.verdict == "verifier_error"
        assert result.error is not None and "symlink" in result.error

    def test_symlinked_task_directory_is_refused(self, tmp_path: Path) -> None:
        """The task-dir leaf is controlled and must not redirect the oracle."""
        outside = _task_with(
            tmp_path,
            {"answer_type": "count", "answer": 3},
            {"answer": 3},
        )
        task_link = tmp_path / "task-link"
        task_link.symlink_to(outside, target_is_directory=True)

        result = ArtifactScorer().score("", task_link)

        assert result.verdict == "verifier_error"
        assert result.error is not None and "symlink" in result.error

    def test_symlinked_task_directory_ancestor_is_refused(
        self, tmp_path: Path
    ) -> None:
        """No supplied task-path component may redirect the oracle lookup."""
        outside_parent = tmp_path / "outside-parent"
        outside = _task_with(
            outside_parent,
            {"answer_type": "count", "answer": 3},
            {"answer": 3},
        )
        controlled = tmp_path / "controlled"
        controlled.mkdir()
        ancestor_link = controlled / "ancestor-link"
        ancestor_link.symlink_to(outside.parent, target_is_directory=True)
        task_through_link = ancestor_link / outside.name

        result = ArtifactScorer().score("", task_through_link)

        assert result.verdict == "verifier_error"
        assert result.error is not None and "symlink" in result.error

    def test_parent_traversal_in_task_path_is_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Relative task paths cannot escape their pinned cwd with ``..``."""
        outside = _task_with(
            tmp_path / "outside-parent",
            {"answer_type": "count", "answer": 3},
            {"answer": 3},
        )
        controlled = tmp_path / "controlled"
        controlled.mkdir()
        monkeypatch.chdir(controlled)
        task_through_parent = Path("..") / outside.parent.name / outside.name

        result = ArtifactScorer().score("", task_through_parent)

        assert result.verdict == "verifier_error"
        assert result.error is not None and "'..'" in result.error

    def test_normal_relative_task_path_still_loads(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        task_dir = _task_with(
            tmp_path,
            {"answer_type": "count", "answer": 3},
            {"answer": 3},
        )
        monkeypatch.chdir(tmp_path)

        result = ArtifactScorer().score("", Path(task_dir.name))

        assert result.score == 1.0
        assert result.verdict is None

    def test_deeply_nested_oracle_is_a_verifier_error(self, tmp_path: Path) -> None:
        """Parser recursion exhaustion is malformed input, not a scorer crash."""
        task_dir = tmp_path / "deep-task"
        (task_dir / "tests").mkdir(parents=True)
        (task_dir / "tests" / "ground_truth.json").write_text(
            "[" * 4000 + "0" + "]" * 4000
        )

        result = ArtifactScorer().score("", task_dir)

        assert result.verdict == "verifier_error"
        assert result.error is not None and "invalid JSON" in result.error

    def test_regular_oracle_still_loads(self, tmp_path: Path) -> None:
        """The hardening must not break the ordinary path."""

        gt = tmp_path / "ground_truth.json"
        gt.write_text(json.dumps({"answer_type": "count", "answer": 3}))

        data, problem = load_ground_truth(tmp_path)
        assert problem is None
        assert data == {"answer_type": "count", "answer": 3}


class TestV2ChecksFormatGetsTheSameGate:
    """The v2 'checks' format must not repeat the v1 bug.

    A check with an unscoreable answer_type used to fold a silent 0.0 into
    the weighted composite, so an agent answering every scoreable check
    correctly still lost that check's weight to a broken oracle.
    """

    def test_rejects_unregistered_answer_type_in_a_check(self) -> None:
        gt = {
            "checks": [
                {"answer_type": "integer", "answer": 5, "weight": 0.5},
                {"answer_type": "count", "answer": 3, "weight": 0.5},
            ]
        }
        problem = validate_ground_truth(gt)
        assert problem is not None
        assert "check[0]" in problem and "integer" in problem

    def test_correct_answers_are_not_charged_for_a_broken_check(
        self, tmp_path: Path
    ) -> None:
        """Both checks answered exactly right must not yield a 0.5 loss."""
        gt = {
            "checks": [
                {"answer_type": "integer", "answer": 5, "weight": 0.5},
                {"answer_type": "count", "answer": 3, "weight": 0.5},
            ]
        }
        task_dir = _task_with(tmp_path, gt, {"answer": 5})
        result = ArtifactScorer().score("", task_dir)
        assert result.verdict == "verifier_error"

    def test_valid_v2_checks_still_score(self, tmp_path: Path) -> None:
        gt = {
            "checks": [
                {"answer_type": "count", "answer": 3, "weight": 0.5},
                {"answer_type": "text", "answer": "x", "weight": 0.5},
            ]
        }
        assert validate_ground_truth(gt) is None
