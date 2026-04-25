# Sourcegraph MCP Tools

> Use Sourcegraph MCP tools as your PRIMARY code search method.
> The repository is indexed at `{{sg_repo}}` on Sourcegraph.
> {{sg_task_search_guidance}}

## Available Tools

| Tool                  | When to use                                                       |
| --------------------- | ----------------------------------------------------------------- |
| `sg_keyword_search`   | Exact keyword/symbol search across the indexed repo               |
| `sg_nls_search`       | Semantic/natural-language code search when keywords aren't enough |
| `sg_find_references`  | Find all usages of a symbol (compiler-accurate, cross-file)       |
| `sg_go_to_definition` | Jump to where a symbol is defined                                 |
| `sg_read_file`        | Read a file from the indexed repo                                 |
| `sg_list_files`       | List files/directories in the repo                                |
| `sg_commit_search`    | Search commit messages and history                                |
| `sg_diff_search`      | Search code changes (added/removed lines)                         |
| `sg_deepsearch`       | Complex cross-file questions requiring multi-step reasoning       |

## Tool Selection

1. **Start with `sg_keyword_search`** for known identifiers (function names, class names, constants)
2. **Use `sg_nls_search`** when you need semantic matching ("error handling code", "authentication logic")
3. **Use `sg_find_references`** to trace all callers/usages of a specific symbol — this catches aliases, re-exports, and indirect imports that grep misses. {{sg_find_references_guidance}}
4. **Use `sg_go_to_definition`** to navigate from a usage to its definition
5. **Fall back to `sg_deepsearch`** for complex cross-file questions

## Scoping

Always scope queries to the target repository:

```
repo:^{{sg_repo}}$ <your query>
```

Start narrow, then broaden if results are insufficient.

## Required Workflow

1. **Search with Sourcegraph** to find files matching the task criteria
2. **Trace references** with `sg_find_references` on key symbols to discover indirect usages
3. {{sg_local_search_step}}
4. {{sg_result_synthesis_step}}
