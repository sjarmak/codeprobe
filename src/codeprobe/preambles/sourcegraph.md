# Sourcegraph MCP Code Access

Sourcegraph MCP tools provide read-only indexed code access for repository
inspection, reference tracing, and cross-repo lookup.

{{repo_scope}}

## Source Access Policy

{{source_access_policy}}

Treat Sourcegraph as the remote read-only index. Use workspace writes only for
required artifacts or code edits.

## Required Workflow

1. **Search with MCP** - Use `mcp__sourcegraph__keyword_search` for exact
   identifiers, literals, error messages, configuration keys, and other known
   terms.
2. **Read with bounded ranges** - Use `mcp__sourcegraph__read_file` with
   `startLine` and `endLine` to fetch only the relevant region. Search results
   include line numbers; pass roughly 20 lines around a hit by default.
{{workflow_tail}}

## Tool Selection

| Goal | Tool |
|------|------|
| Exact terms (AND logic, all must match) | `mcp__sourcegraph__keyword_search` |
| Broader matching (OR logic + word stemming) | `mcp__sourcegraph__nls_search` |
| Trace usage/callers | `mcp__sourcegraph__find_references` |
| See implementation | `mcp__sourcegraph__go_to_definition` |
| Read a code region (default) | `mcp__sourcegraph__read_file` with `startLine`/`endLine` |
| Read a full small file (rare) | `mcp__sourcegraph__read_file` with no range |
| Browse structure | `mcp__sourcegraph__list_files` |
| Find repositories | `mcp__sourcegraph__list_repos` |
| Search commits | `mcp__sourcegraph__commit_search` |
| Track changes | `mcp__sourcegraph__diff_search` |
| Compare versions | `mcp__sourcegraph__compare_revisions` |

**Decision logic:**
1. Know the exact symbol or string? Use `mcp__sourcegraph__keyword_search`.
2. Need word-stem variants such as "authenticate", "authentication", and
   "authenticator"? Use `mcp__sourcegraph__nls_search` with extracted keywords,
   not a full natural-language question.
3. Need definition of a symbol? Use `mcp__sourcegraph__go_to_definition`.
4. Need all callers or references? Use `mcp__sourcegraph__find_references`.
5. Need a specific code region? Use `mcp__sourcegraph__read_file` with
   `startLine` and `endLine`.
6. Need broad file structure? Use `mcp__sourcegraph__list_files`.

## Scoping

```
repo:^github.com/ORG/REPO$      # Exact repository
repo:github.com/ORG/            # Organization scope
file:.*\.ts$                    # TypeScript only
file:src/api/                   # Directory scope
```

Start narrow. Expand only when results are empty or clearly incomplete.

## Query Construction

Both `mcp__sourcegraph__keyword_search` and `mcp__sourcegraph__nls_search`
expect extracted keywords, not full questions. Strip question words and
articles.

- "how does the router match incoming requests to handlers"
- "router match request handler"

`mcp__sourcegraph__nls_search` applies stemming to a single root form, so
"handle" already covers "handler", "handling", and "handles".

## Efficiency Rules

- Chain searches logically: search, read range, references, definition.
- Do not re-search for the same pattern; use results from prior calls.
- Prefer `mcp__sourcegraph__keyword_search` when exact terms are known; fall
  back to `mcp__sourcegraph__nls_search` only when exact search returns too few
  hits.
- Default to range-bounded reads. Search snippets carry line numbers; pass
  `startLine` and `endLine` to `mcp__sourcegraph__read_file` instead of fetching
  whole files. Reserve full-file reads for cases that need broad structure.
- Read 2-3 related code regions before synthesizing, rather than one at a time.
- Do not read 20+ remote regions without writing code or answer text. Once the
  pattern is clear, act.
- Agent turns are capped per task. Repeated `mcp__sourcegraph__read_file` calls
  without progress can exhaust the cap.

## If Stuck

If MCP search returns no results:
1. Broaden the query by dropping a term or trying root forms.
2. Switch from `mcp__sourcegraph__keyword_search` to
   `mcp__sourcegraph__nls_search`.
3. Use `mcp__sourcegraph__list_files` to browse directory structure.
4. Use `mcp__sourcegraph__list_repos` to verify the repository name.
