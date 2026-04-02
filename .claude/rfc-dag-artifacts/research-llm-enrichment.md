# Research: LLM Enrichment

## Pipeline Flow

1. `mine_tasks()` in `extractor.py` iterates merged PRs
2. For each PR: extracts changed files, resolves PR metadata (API/commit/bare)
3. Calls `score_pr_quality()` which returns 0.0-1.0 based on 4 structural signals
4. Quality score is used to filter (min_quality threshold) but NOT persisted
5. Tasks are sorted by quality, truncated to count, returned as `list[Task]`
6. `run_mine()` in `mine_cmd.py` calls `mine_tasks()` then `write_task_dir()` for each

## Key Data Structures

- `TaskMetadata` (frozen dataclass in `models/task.py`): lacks `quality_score` and `enrichment_source` fields
- `PRMetadata` (frozen dataclass in `extractor.py`): has `body`, `title`, `source_tier`
- `call_claude(LLMRequest) -> LLMResponse` in `core/llm.py`: takes prompt+model, returns text

## Enrichment Insertion Point

Between `mine_tasks()` returning and `write_task_dir()` writing — in `run_mine()`.
Alternatively, `mine_tasks()` could return quality scores alongside tasks.

## Current Quality Score Gap

`mine_tasks()` computes quality but discards it after filtering. Need to either:

- Return it alongside tasks, OR
- Recompute in `run_mine()`

Best approach: return `(quality, task)` tuples from `mine_tasks()`.
