# IMPORTANT: Source Code Access

**Local source files are not present.** Your workspace does not contain source code. You **MUST** use Sourcegraph MCP tools to discover, read, and understand code before making any changes.

{{repo_scope}}

## Required Workflow

1. **Search first** — Use MCP tools to find relevant files and understand existing patterns
2. **Read remotely with line ranges** — Use `sg_read_file` with `startLine`/`endLine` to fetch only the relevant region. Search results include line numbers; pass them ±~20 lines as a range.
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

- Chain searches logically: search → read range → references → definition
- Don't re-search for the same pattern; use results from prior calls
- Prefer `sg_keyword_search` when you have exact terms; fall back to `sg_nls_search` only when keyword search returns too few hits
- Default to range-bounded reads. Search snippets carry line numbers — pass `startLine`/`endLine` to `sg_read_file` (±~20 lines) instead of fetching whole files. Reserve full-file reads for cases where you need broad structure.
- Read 2-3 related code regions before synthesising, rather than one at a time
- Don't read 20+ remote regions without writing code — once you understand the pattern, start implementing

## If Stuck

If MCP search returns no results:
1. Broaden the query (drop a term, try root forms)
2. Switch from `sg_keyword_search` (AND) to `sg_nls_search` (OR + stemming)
3. Use `sg_list_files` to browse the directory structure
4. Use `sg_list_repos` to verify the repository name
