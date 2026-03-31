# Sourcegraph MCP Guide

> You have access to Sourcegraph MCP tools for code search and navigation.
> Use these tools to explore the codebase at `{{repo_path}}` efficiently.

## Available Sourcegraph MCP Tools

| Tool                  | Purpose                          | Best For                                   |
| --------------------- | -------------------------------- | ------------------------------------------ |
| `sg_keyword_search`   | Fast exact string matching       | Function names, error messages, imports    |
| `sg_nls_search`       | Natural language semantic search | "How does X work?", architecture questions |
| `sg_read_file`        | Read file from indexed repo      | Examining specific files found via search  |
| `sg_list_files`       | Find files by pattern            | Discovering file/directory structure       |
| `sg_go_to_definition` | Navigate to symbol definitions   | Tracing function/type definitions          |
| `sg_find_references`  | Find all references to a symbol  | Understanding usage patterns               |
| `sg_commit_search`    | Search git commit history        | Understanding code evolution               |

## Search Strategy

1. **Start broad**: Use `sg_nls_search` to understand the problem domain
2. **Get specific**: Use `sg_keyword_search` for exact function/class names
3. **Trace definitions**: Use `sg_go_to_definition` for symbol navigation
4. **Check usage**: Use `sg_find_references` to understand impact
5. **Read files**: Use `sg_read_file` for detailed examination

## Recommended Workflow

1. Use Sourcegraph MCP tools to explore and understand the codebase
2. Identify all relevant files and code paths
3. Read specific files locally for detailed examination
4. Make targeted code changes
5. Verify changes with tests
