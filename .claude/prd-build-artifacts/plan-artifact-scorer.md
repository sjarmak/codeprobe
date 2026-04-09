# Plan: ArtifactScorer

## Phase 3 — Implementation

### scoring.py additions

1. Add `ArtifactScorer` class implementing `Scorer` protocol
2. Helper: `_normalize_path(p: str) -> str` — evolved from \_ORACLE_PY normalize()
3. Helper: `_load_json(path: Path) -> dict | None` — safe JSON loader
4. Helper: `_find_answer_file(task_dir: Path) -> Path | None` — try both locations
5. Score methods per answer_type:
   - `_score_file_list(answer, expected) -> float` — F1
   - `_score_count(answer, expected) -> float` — exact int match
   - `_score_boolean(answer, expected) -> float` — case-insensitive
   - `_score_text(answer, expected) -> float` — stripped lowercase
6. Format detection: `"answer_type" in gt` → new format, else legacy

### registry.py

- Add `"artifact": "codeprobe.core.scoring:ArtifactScorer"` to `_SCORER_BUILTINS`

### core/**main**.py (new)

- `python -m codeprobe.core.scoring --artifact <task_dir>` entry point
- Actually: `python -m codeprobe.core` with argparse

Wait — the acceptance criteria says `python -m codeprobe.core.scoring --artifact <task_dir>`.
So add `if __name__ == "__main__"` block to scoring.py itself.

### get_scorer update

- Update type hint to include ArtifactScorer

## Phase 4 — Tests

- test_artifact_scorer.py with tmp_path fixtures
- Test all 4 answer_types
- Test legacy format
- Test confidence < 0.5 warning
- Test missing answer.json
- Test missing ground_truth.json
