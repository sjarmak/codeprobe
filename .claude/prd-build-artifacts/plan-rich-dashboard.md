# Plan: rich-dashboard

## Step 1: Add rich dependency to pyproject.toml

- Add `"rich>=13.7,<14"` to the dependencies list

## Step 2: Create src/codeprobe/cli/rich_display.py

- RichLiveListener class implementing RunEventListener protocol
- **init**: Console(stderr=True), state tracking fields
- on_event dispatching to handler methods per event type
- \_build_table() method returns a Rich Table with progress info
- Live context managed on RunStarted, stopped on RunFinished
- Thread-safe: call self.live.update(self.\_build_table()) from on_event

## Step 3: Add \_should_use_rich() to run_cmd.py

- Check sys.stderr.isatty(), CI env vars, TERM != dumb

## Step 4: Add --force-plain and --force-rich flags

- Add to cli/**init**.py run command click options
- Pass through to run_eval()
- Update run_eval signature

## Step 5: Modify \_run_config in run_cmd.py

- Accept force_plain/force_rich params in run_eval
- Conditional listener registration based on TTY detection and flags
- Wrap execute_config in try/except KeyboardInterrupt for clean shutdown

## Step 6: Test

- pytest tests/ -x -q
- Verify imports work, \_should_use_rich returns False with CI=true
