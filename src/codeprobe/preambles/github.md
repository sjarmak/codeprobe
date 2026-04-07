# GitHub MCP Tools

> Use GitHub MCP tools to search code, issues, and pull requests.
> The repository is `{{sg_repo}}` (or derive from the local repo name: `{{repo_name}}`).

## Available Tools

| Tool                  | When to use                                            |
| --------------------- | ------------------------------------------------------ |
| `search_code`         | Search code across the repository by keyword or symbol |
| `search_repositories` | Find repositories by name or topic                     |
| `get_file_contents`   | Read a file from the repository at a specific ref      |
| `search_issues`       | Search issues and pull requests                        |
| `list_commits`        | List recent commits on a branch                        |

## Required Workflow

1. **Search with `search_code`** to find files matching the task criteria
2. **Read files** with `get_file_contents` to verify matches and trace imports
3. **Supplement with local Grep** to catch anything the API may have missed
4. **Union all results** — combine both approaches for maximum recall
