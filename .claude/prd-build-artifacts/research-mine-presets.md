# Research: mine-presets

## mine_cmd.py

- `run_mine()` accepts: path, count, source, min_files, subsystems, discover_subsystems, enrich, interactive, no_llm, org_scale, families, repos, scan_timeout, validate_flag, curate, backends, verify_curation_flag, mcp_families, sg_repo
- CLI wiring in `cli/__init__.py` lines 102-267: @click decorators define all flags
- No preset system exists; all flags are independent
- No help group formatting exists

## init_cmd.py

- `_MCP_SEARCH_PATHS` (lines 103-108): list of Path objects for known MCP config locations
- `_discover_mcp_configs()` (lines 111-137): scans those paths + cwd/.mcp.json, returns list[(Path, list[str])]
- Used by `_prompt_mcp_config()` and `_goal_mcp()` within init_cmd
- Also `_detect_sourcegraph_in_mcp()` depends on the discovered list

## experiment_cmd.py

- `experiment_add_config()` (lines 59-137): takes mcp_config_str param
- CLI wiring at `cli/__init__.py` lines 355-400: `--mcp-config` is optional, no auto-discovery
- No interactive MCP discovery when --mcp-config is omitted

## Key decisions

- Extract `_MCP_SEARCH_PATHS` and `_discover_mcp_configs` into `core/mcp_discovery.py`
- init_cmd.py imports from core.mcp_discovery
- experiment_cmd.py uses discover_mcp_configs() for interactive selection when --mcp-config missing and TTY
- mine_cmd.py gets --preset with PRESETS dict, explicit flags override
