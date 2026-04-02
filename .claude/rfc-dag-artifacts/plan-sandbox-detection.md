# Plan: sandbox-detection

## Step 1: Create `src/codeprobe/core/sandbox.py`

- Function `is_sandboxed() -> bool`
- Check 1: `Path("/.dockerenv").exists()`
- Check 2: `os.environ.get("CODEPROBE_SANDBOX") == "1"`
- Check 3: Read `/proc/1/cgroup`, check for "docker" or "containerd" in contents (handle FileNotFoundError/PermissionError gracefully)
- Return True if any check passes, False otherwise

## Step 2: Update `src/codeprobe/adapters/protocol.py`

- Add `"dangerously_skip"` to `ALLOWED_PERMISSION_MODES` frozenset

## Step 3: Update `src/codeprobe/adapters/claude.py`

- Import `is_sandboxed` from `codeprobe.core.sandbox`
- Override `preflight()`: call super, then if `permission_mode == "dangerously_skip"` and `not is_sandboxed()`, append error string containing "sandboxed environment"
- Update `build_command()`: when `permission_mode == "dangerously_skip"`, append `--dangerously-skip-permissions` flag instead of `--permission-mode dangerously_skip`

## Step 4: Write tests in `tests/test_adapters.py`

- `TestSandboxDetection` class:
  - `test_is_sandboxed_via_dockerenv`: mock `Path.exists` for `/.dockerenv` -> True
  - `test_is_sandboxed_via_env_var`: set `CODEPROBE_SANDBOX=1` in os.environ
  - `test_is_sandboxed_via_cgroup`: mock `open` for `/proc/1/cgroup` containing "docker"
  - `test_is_sandboxed_via_cgroup_containerd`: mock with "containerd"
  - `test_not_sandboxed_bare_host`: all signals absent -> False
- `TestClaudeSandboxGating` class:
  - `test_preflight_rejects_dangerously_skip_outside_sandbox`: mock `is_sandboxed` -> False, check error string
  - `test_preflight_allows_dangerously_skip_in_sandbox`: mock `is_sandboxed` -> True, check no sandbox error
  - `test_build_command_includes_skip_flag_in_sandbox`: mock binary + `is_sandboxed` -> True, verify flag
