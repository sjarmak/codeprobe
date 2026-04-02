# Plan: LLM Enrichment

## Changes

### 1. models/task.py — Add fields to TaskMetadata

- `quality_score: float = 0.0`
- `enrichment_source: str = ""` (values: "", "llm")

### 2. extractor.py — Return quality scores

- Change `mine_tasks()` to attach quality_score to each Task's metadata
- Create `enrich_task()` function that calls `call_claude()` with enrichment prompt
- Create `enrich_tasks()` that filters for quality < 0.5 and enriches them

### 3. mine_cmd.py — Add --enrich flag

- Add `enrich: bool = False` param to `run_mine()`
- When True, call `enrich_tasks()` on the mined tasks before writing

### 4. cli/**init**.py — Wire --enrich click option

### 5. tests/test_mining.py — Add enrichment tests

- Mock `call_claude()`
- Verify low-quality tasks get enriched
- Verify high-quality tasks pass through unchanged
- Verify metadata fields

## Enrichment Prompt Design

Ask for: (a) clear problem statement, (b) acceptance criteria, (c) difficulty assessment.
Include: PR title, body, commit messages.
