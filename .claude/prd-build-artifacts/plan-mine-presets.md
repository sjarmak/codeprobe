# Plan: mine-presets

## Step 1: Create core/mcp_discovery.py

- Move `_MCP_SEARCH_PATHS` and `_discover_mcp_configs()` from init_cmd.py
- Export as `MCP_SEARCH_PATHS` and `discover_mcp_configs()` (public names)
- Keep function signature identical

## Step 2: Update init_cmd.py

- Remove `_MCP_SEARCH_PATHS` and `_discover_mcp_configs()` definitions
- Add `from codeprobe.core.mcp_discovery import MCP_SEARCH_PATHS, discover_mcp_configs`
- Update all references: `_MCP_SEARCH_PATHS` -> `MCP_SEARCH_PATHS`, `_discover_mcp_configs` -> `discover_mcp_configs`

## Step 3: Add --preset to mine command

- In cli/**init**.py: add `@click.option("--preset", ...)` to the mine command
- In mine_cmd.py: define PRESETS dict, apply in run_mine() before any logic
- Explicit CLI flags override preset values (check if value differs from default)
- Add help formatter customization for flag groups

## Step 4: Update experiment_cmd.py add-config

- In `experiment_add_config()`: when mcp_config_str is None and sys.stderr.isatty(), call discover_mcp_configs() and present interactive selection

## Step 5: Write tests

- tests/test_mine_presets.py with CliRunner tests for preset behavior
- Verify existing tests pass (test_init_wizard.py)
