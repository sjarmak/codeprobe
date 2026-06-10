# IMPORTANT: Source Code Access

Sourcegraph MCP tools give you read access to code that is **not** in your local workspace. Use them to discover, read, and understand cross-repo code before making changes.

{{repo_scope}}

## Workspace Source Priority (READ FIRST)

**If a file exists locally in your workspace, use local `Read` / `Grep` / `Glob` / `Bash` — do NOT call `sg_read_file` for it.** MCP round-trips are 10-100× slower and more expensive than local reads, and the local copy is the source of truth for edits.

Decision rule before any `sg_read_file` call:

1. Is the path inside your workspace? Run `Glob` or `ls` to check.
2. If yes → use local `Read`. Stop.
3. If no (cross-repo lookup, or workspace is sg-only / scaffolded) → use `sg_read_file`.

Treat Sourcegraph as a **remote read-only index**, not a substitute for the filesystem in front of you. The MCP tools are for code you cannot otherwise see.

## Required Workflow

1. **Check local first** — Run `Glob`/`Read` on the workspace before reaching for MCP. Only use MCP when the file is genuinely not on disk.
2. **Search remotely** — Use MCP search tools (`sg_keyword_search`, `sg_nls_search`) to find relevant files in repos that aren't checked out locally.
3. **Read remotely with line ranges** — Use `sg_read_file` with `startLine`/`endLine` to fetch only the relevant region. Search results include line numbers; pass them ±~20 lines as a range.
{{workflow_tail}}

## Tool Selection

| Goal | Tool |
|------|------|
| Exact terms (AND logic, all must match) | `sg_keyword_search` |
| Broader matching (OR logic + word stemming) | `sg_nls_search` |
| Trace usage/callers | `sg_find_references` |
| See implementation | `sg_go_to_definition` |
| Read a code region (default) | `sg_read_file` with `startLine`/`endLine` |
| Read full small file (rare) | `sg_read_file` (no range) |
| Browse structure | `sg_list_files` |
| Find repos | `sg_list_repos` |
| Search commits | `sg_commit_search` |
| Track changes | `sg_diff_search` |
| Compare versions | `sg_compare_revisions` |

**Decision logic:**
1. Know the exact symbol or string? → `sg_keyword_search` (all terms must match)
2. Don't know exact names, or want word-stem variants ("authenticate" also matches "authentication", "authenticator")? → `sg_nls_search` (any term matches; pass extracted keywords, NOT natural-language questions)
3. Need definition of a symbol? → `sg_go_to_definition`
4. Need all callers/references? → `sg_find_references`
5. Need a specific code region? → `sg_read_file` with `startLine`/`endLine` (default — use ±20 lines around the search hit)
6. Need a full small file? → `sg_read_file` with no range (only when you need broad structure; files >128KB are auto-truncated to 200 lines)

## Scoping (Always Do This)

```
repo:^github.com/ORG/REPO$      # Exact repo (preferred)
repo:github.com/ORG/            # All repos in org
file:.*\.ts$                    # TypeScript only
file:src/api/                   # Specific directory
```

Start narrow. Expand only if results are empty.

## Query Construction

Both `sg_keyword_search` and `sg_nls_search` expect **extracted keywords**, not full questions. Strip question words (how, what, where, does, is) and articles (the, a, an).

- "how does the router match incoming requests to handlers"
- "router match request handler"

`sg_nls_search` applies stemming to a single root form, so "handle" already covers "handler" / "handling" / "handles".

## Efficiency Rules

- **Never** call `sg_read_file` for a path that exists locally — use `Read`. (See "Workspace Source Priority" above.)
- Chain searches logically: search → read range → references → definition
- Don't re-search for the same pattern; use results from prior calls
- Prefer `sg_keyword_search` when you have exact terms; fall back to `sg_nls_search` only when keyword search returns too few hits
- Default to range-bounded reads. Search snippets carry line numbers — pass `startLine`/`endLine` to `sg_read_file` (±~20 lines) instead of fetching whole files. Reserve full-file reads for cases where you need broad structure.
- Read 2-3 related code regions before synthesising, rather than one at a time
- Don't read 20+ remote regions without writing code — once you understand the pattern, start implementing
- Agent turns are capped per task (see experiment config). Looping `sg_read_file` calls without progressing toward edits will exhaust the cap.

## If Stuck

If MCP search returns no results:
1. Broaden the query (drop a term, try root forms)
2. Switch from `sg_keyword_search` (AND) to `sg_nls_search` (OR + stemming)
3. Use `sg_list_files` to browse the directory structure
4. Use `sg_list_repos` to verify the repository name
