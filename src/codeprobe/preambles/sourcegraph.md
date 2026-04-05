# Sourcegraph MCP — REQUIRED

> **IMPORTANT**: You MUST use Sourcegraph MCP tools as your PRIMARY search method.
> The repository is indexed at `github.com/sg-evals/{{repo_name}}` on Sourcegraph.
> Use Sourcegraph `find_references` and `keyword_search` FIRST, then supplement with local Grep.

## Available Tools

| Tool               | What it does                                    |
| ------------------ | ----------------------------------------------- |
| `keyword_search`   | Exact keyword search across the indexed repo    |
| `nls_search`       | Semantic/natural-language code search           |
| `find_references`  | Find all usages of a symbol (compiler-accurate) |
| `go_to_definition` | Jump to where a symbol is defined               |
| `read_file`        | Read a file from the indexed repo               |
| `list_files`       | List files/directories in the repo              |
| `commit_search`    | Search commit messages and history              |
| `diff_search`      | Search code changes (added/removed lines)       |

## Required Workflow

1. **Use `keyword_search`** with `repo:github.com/sg-evals/{{repo_name}}` to find files matching the task criteria
2. **Use `find_references`** on key symbols to trace callers and usages across files
3. **Supplement with local Grep** to catch anything Sourcegraph may have missed
4. **Combine all results** into the final answer — union of both approaches for best recall
