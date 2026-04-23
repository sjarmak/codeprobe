# Snapshot Redaction

`codeprobe snapshot create` produces a shareable snapshot of an experiment
directory. The default mode is **hashes-only** — the snapshot contains only
per-file `sha256 + size`, never any file bodies. Modes that include file
bodies require an explicit `--allow-source-in-export` opt-in and, for secret
inclusion, a pre-publish canary gate.

No LLM is invoked anywhere in the redaction path. All scanning is done by
deterministic tools: `gitleaks`, `trufflehog`, or user-configurable regex
patterns via the built-in `PatternScanner`.

## Capability matrix

| Mode | Bodies in snapshot? | Requires `--allow-source-in-export`? | Requires canary gate? | Public default? |
| ---- | ------------------- | ------------------------------------ | --------------------- | --------------- |
| `hashes-only` | No — only `sha256 + size` per file | No | No | **Yes** |
| `contents` | Yes — bodies are piped through `scanner.redact(bytes)` before being written | Yes | No | No |
| `secrets` | Yes — same as `contents` | Yes | **Yes** | No |

> `--redact=none` is **not** available from the publishable CLI surface. Use
> `hashes-only` for shareable output, or consume the experiment directory
> directly for local-only previews.

## Per-mode guarantees

### `hashes-only` (default)

- Walks the source directory and records `{path, sha256, size}` for every
  file into `SNAPSHOT.json`.
- No file bodies are copied. Grepping the snapshot for any source-file
  symbol returns zero hits.
- Suitable for: cross-org benchmarking, attestation of a reproducible source
  tree, supply-chain receipts.

### `contents`

- Bodies are copied to `<out>/files/<relative-path>` but each body is passed
  through the configured scanner's `redact(bytes)` before being written.
- Requires `--allow-source-in-export`. Without this flag the CLI exits
  non-zero and refuses to run.
- Scanner default is `pattern` (regex scanner with a built-in rule list).
  Pass `--scanner gitleaks` or `--scanner trufflehog` to use external tools
  (binary must be on `PATH`).

### `secrets`

- Same as `contents`, **and** the scanner must prove — via the canary gate —
  that it would catch a known canary string before the snapshot is written.
- The gate may be satisfied two ways:
  - **Interactive**: run in a TTY and paste the canary string when prompted.
    The CLI then plants the canary, runs the scanner, and requires a hit.
  - **Non-interactive**: pass `--canary-proof <path.json>` pointing at a
    previously-recorded `CanaryResult` with `passed=true`.
- If the scanner fails to catch the planted canary the snapshot is aborted.

## Signed attestation

Every `SNAPSHOT.json` carries an `attestation` block:

```json
{
  "attestation": {
    "kind": "hmac-sha256",
    "signature": "<hex>",
    "body_sha256": "<hex>",
    "redaction_mode": "hashes-only",
    "scanner_name": null,
    "canary": null,
    "timestamp": "2026-04-22T00:00:00+00:00"
  }
}
```

- When `CODEPROBE_SIGNING_KEY` is set (or `--signing-key` is passed), the
  manifest body is signed via HMAC-SHA256.
- When no key is available, the manifest is written with
  `attestation.kind = "unsigned"`; the `body_sha256` is still recorded for
  tamper detection.

**Production deployments MUST manage `CODEPROBE_SIGNING_KEY`** — an unsigned
attestation is informational only. Per-tenant or per-environment keys are
recommended; rotate them the same way you rotate any other signing secret.

## Verify a snapshot

```bash
codeprobe snapshot verify path/to/snapshot
# exit 0 if body_sha256 matches and (if signed) signature verifies
```

## Scanner customization

All scanners implement the same `Scanner` protocol
(`src/codeprobe/snapshot/scanners.py`):

```python
class Scanner(Protocol):
    name: str
    def scan(self, data: bytes) -> list[Finding]: ...
    def redact(self, data: bytes) -> bytes: ...
```

`PatternScanner(patterns=...)` accepts a list of
`(rule_id, compiled_byte_regex)` tuples so operators can extend the built-in
rule set with organization-specific secret formats without patching
codeprobe.
