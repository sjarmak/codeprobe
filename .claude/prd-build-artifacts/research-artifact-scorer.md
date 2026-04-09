# Research: ArtifactScorer

## Existing Code

### scoring.py

- `Scorer` Protocol: `score(agent_output: str, task_dir: Path) -> ScoreResult`
- `ScoreResult(frozen=True)`: `score: float, passed: bool, error: str | None = None`
- Three existing scorers: BinaryScorer, ContinuousScorer, CheckpointScorer
- Registry in registry.py maps names to classes via `_SCORER_BUILTINS`

### mining/writer.py \_ORACLE_PY (lines 67-118)

- F1 oracle scorer for file lists
- `normalize()` strips prefix paths (`./`, `/workspace/`, `/tmp/`, `/app/`) and backslashes
- Reads `ground_truth.json` with `expected` key (list of file paths)
- Computes precision, recall, F1 from set intersection
- Writes reward.txt with F1 score

## Ground Truth Schemas

### Legacy format

```json
{
  "schema_version": 1,
  "oracle_type": "file_list",
  "expected": ["path/a.py", "path/b.py"]
}
```

Detect: absence of `answer_type` key.

### New format

```json
{"answer_type": "file_list|count|boolean|text", "answer": ..., "confidence": 0.9, "provenance": "..."}
```

Detect: presence of `answer_type` key.

## Answer Types

1. `file_list` — F1 score (precision/recall of normalized paths)
2. `count` — exact integer match → 1.0 or 0.0
3. `boolean` — case-insensitive match → 1.0 or 0.0
4. `text` — stripped lowercase exact match → 1.0 or 0.0

## Answer File Location

Try `task_dir / "answer.json"` first, then `task_dir / "tests" / "answer.json"`.
