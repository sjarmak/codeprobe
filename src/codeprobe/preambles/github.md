# GitHub MCP Tools

> Use GitHub-backed MCP tools as your PRIMARY code search and navigation method
> for the repository `{{sg_repo}}` (or, if that template variable is not set,
> fall back to the local repo name `{{repo_name}}`).
>
> Supplement the indexed tools with local Grep for best recall. The exact tool
> names are provided by your MCP configuration at run time — this preamble
> describes the *capabilities* you should lean on, not the specific tool names.

## Capabilities in this environment

The MCP server connected to this session exposes the following code-intelligence
capabilities. Prefer these over ad-hoc shell commands whenever a relevant tool
is available.

- **KEYWORD_SEARCH** — Exact identifier and literal search across the indexed
  repository. Use this first for known function names, class names, error
  message strings, configuration keys, and other unambiguous tokens.

- **SYMBOL_REFERENCES** — Compiler- or indexer-accurate find-usages for a
  named symbol across all files. Use this to trace callers, implementers,
  re-exports, and indirect imports that a raw keyword search would miss.

- **FILE_READ** — Read a specific file at a specific ref from the indexed
  repository. Prefer this over `cat`/local reads when you need to confirm the
  current state of upstream code without a local checkout.

- **GO_TO_DEFINITION** — Jump from a symbol usage to its definition site.
  Use this to follow the call chain without guessing at file paths.

## Repository scoping

All queries should be scoped to the target repository when the backing provider
supports it. The GitHub code-search surface and the MCP layer on top of it both
honor a repository filter — pass the repo identifier (`{{sg_repo}}` when
available, otherwise `{{repo_name}}`) into every KEYWORD_SEARCH call so results
are not diluted by unrelated repos.

Start narrow — single-repo, single-path — and broaden scope only when the
initial result set is clearly incomplete. Broad cross-repo searches are
expensive and noisy.

## Required workflow

1. **Start with KEYWORD_SEARCH** for any identifier you already know by name.
   This is the fastest path to a short candidate list of files.

2. **Trace with SYMBOL_REFERENCES** on each promising symbol discovered in
   step 1. Keyword search will miss aliases, wrapper re-exports, and runtime
   dispatch — reference tracing catches them.

3. **Confirm with FILE_READ** by reading each candidate file at the ground-truth
   ref the task provides. Do not rely on a stale local copy if the task
   specifies a commit.

4. **Navigate with GO_TO_DEFINITION** when a call chain is the answer the task
   is asking about — keyword search alone will drop you at the first match,
   which may not be the canonical definition.

5. **Supplement with local Grep** as a final sweep to catch anything the
   indexed tools may have missed (new files, unindexed branches, comments
   that the symbol index skipped).

6. **Union all results** — combine indexed search and local Grep before you
   decide the candidate set is complete. Under-recall is the most common
   failure mode for this class of task; over-report and then filter.

## Output discipline

When the task asks for a list of files, symbols, or call sites, return a
deterministic, deduplicated result set. Record which capability produced each
finding (KEYWORD_SEARCH vs. SYMBOL_REFERENCES vs. local Grep) so the final
answer is auditable. When two capabilities disagree, prefer the
compiler-accurate one (SYMBOL_REFERENCES, GO_TO_DEFINITION) over
string-matching (KEYWORD_SEARCH, Grep).
