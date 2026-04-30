# benchmark_qa_core

Schema-agnostic Python library for cross-benchmark task QA, shared between the
codeprobe rig and the EnterpriseBench (EB) and CodeScaleBench (CSB) sister
benchmarks.

* Source: `src/codeprobe/qa/benchmark_qa_core/`
* Public API: `codeprobe.qa.benchmark_qa_core`
* Tests: `src/codeprobe/qa/benchmark_qa_core/tests/`

The library exposes three pure functions that run mechanical, deterministic
checks on already-parsed task inputs (oracle files, oracle symbols, scoring
metadata, aux-file content). Each rig owns the schema parsing on the way in
and the surface (CLI report, dolt write, etc.) on the way out — the lib only
returns a flat `list[Finding]`.

## Public API

```python
from codeprobe.qa.benchmark_qa_core import (
    Finding,
    OracleConstraints,
    check_oracle_coherence,
    check_scoring_honesty,
    check_aux_file_leakage,
)
```

| Function | Codes | Purpose |
| --- | --- | --- |
| `check_oracle_coherence` | `A1`, `B1`, `B2`, `C1`, `D1`, `D2` | File / symbol / language / path coherence |
| `check_scoring_honesty`  | `E1`, `E2`, `E3`               | Declared scoring method matches a sanctioned tier |
| `check_aux_file_leakage` | `F1`, `F2`, `F3`               | Oracle tokens not present in agent-visible aux files |

Codes are stable across rigs — pin them in waivers and downstream gating with
confidence.

## Vendor process for EB and CSB

`benchmark_qa_core` is **not yet a published Python package**. Until a package
strategy is decided, EB and CSB consume it via vendored copies. The contract
is *file copy* (not a git submodule, not a pip dependency).

### Vendoring into a sister rig

From the **codeprobe** repo working tree, run:

```bash
# In the consumer rig (EB or CSB), choose a destination namespace.
# Conventional path: <rig>/src/<rig>/qa/benchmark_qa_core/
DEST=/path/to/<rig>/src/<rig>/qa/benchmark_qa_core

mkdir -p "$DEST"
cp -r src/codeprobe/qa/benchmark_qa_core/* "$DEST/"

# Tests are part of the vendor copy. Drop them into the consumer rig's test
# tree under the matching namespace so CI runs them on every change.

# Rewrite the package import root from `codeprobe.qa.benchmark_qa_core`
# to `<rig>.qa.benchmark_qa_core` so the vendored copy is self-contained.
grep -rl 'codeprobe.qa.benchmark_qa_core' "$DEST" \
  | xargs sed -i 's/codeprobe\.qa\.benchmark_qa_core/<rig>.qa.benchmark_qa_core/g'
```

Record the source commit SHA in the consumer rig's `VENDOR.md` (or equivalent)
so drift is auditable.

### When this lib changes

> **Vendor copies in EB and CSB MUST be refreshed on every change to
> `src/codeprobe/qa/benchmark_qa_core/`.** Otherwise the three rigs silently
> drift and a finding code can mean different things in different reports.

Suggested process:

1. Land the change in codeprobe with tests passing.
2. Note the commit SHA on `main`.
3. Open follow-up beads in EB and CSB to refresh the vendored copy and
   re-run the consumer test suite.
4. The bead should record the source SHA so reviewers can verify drift was
   closed.

### Why vendor instead of pip-install?

* The library is < 600 lines and has no third-party runtime deps.
* EB and CSB run in different environments; pinning a private package across
  three rigs adds infra cost we haven't yet decided to pay.
* A vendor copy makes each rig's CI self-contained.

If/when this changes, swap in the package-install workflow and delete this
section.

## Adding new checks

1. Add a new module under `src/codeprobe/qa/benchmark_qa_core/`.
2. Allocate a fresh letter prefix for codes (the existing namespaces are A–F).
3. Write tests under `src/codeprobe/qa/benchmark_qa_core/tests/`.
4. Re-export the new check function from `__init__.py`.
5. Update this README's API table.

## Constraints (project rules)

* **Pure functions only.** No agent calls, no network IO, no implicit reads of
  the host filesystem outside paths the caller passed in.
* **Schema-agnostic.** Anything benchmark-specific (task-meta shape, oracle
  format) lives in the rig adapter, not here.
* **Deterministic.** Given the same inputs, the lib returns the same findings
  in the same order on every run.
