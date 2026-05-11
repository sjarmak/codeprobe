# sdlc_sgonly_smoke

Smoke fixture for the codeprobe-2nw2 scaffold-mode integration tests.

This is a **structural smoke harness**, not a real Go project:

- `src/math.go` is pre-populated but has no `go.mod`, no `go test`
  setup, and is never compiled.
- The oracle is `bash tests/test.sh` (which `grep`s for `func add`),
  **not** `go test ./...`. `verification.command` in
  `metadata.json` reflects the bash entry point so codeprobe's
  verifier can actually run it.

The original codeprobe-zgjv bead description specified
`verification.command: "go test ./..."`. That value would crash on a
fixture with no Go toolchain wiring — see
`docs/investigations/codeprobe-2nw2/design.md` §"Validation
walk-through" for why the bash oracle was chosen instead. The
`metadata.json` field is the source of truth for what the verifier
runs; `tests/test.sh` is the actual check.

The fixture exists so codeprobe-yw6u (.2), codeprobe-sm9f (.3), and
codeprobe-hcnv (.4) can implement scaffold mode against a stable
target. It is **not** intended to be run through
`codeprobe run` or `score_tasks_dir` directly — its narrow
ground-truth (`oracle_answer: ["src/math.go"]`) would score below
the curator's promotion threshold.
