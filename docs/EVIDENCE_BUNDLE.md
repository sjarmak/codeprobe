# Zero-Code-Access Evidence Bundle

`codeprobe snapshot evidence` prepares a fixed bundle for leaving an
environment controlled by the data owner without granting the reviewer source
access. It is a separate, stricter boundary than `snapshot create`: it never
exports file names, file sizes, source bodies, prompts, patches, traces,
task-level results, raw diagnostics, or free-form identifying text.

The data owner controls both steps:

```bash
codeprobe snapshot evidence preview request.json --no-json
codeprobe snapshot evidence export request.json \
  --out approved-evidence \
  --approve sha256:<digest-from-the-reviewed-preview> \
  --no-json
```

Preview performs no writes. It prints the exact five proposed artifacts and an
approval digest bound to their normalized content, including every schema
version and allowed value. Export rebuilds and validates the preview, compares
the supplied digest in constant time, and atomically publishes a new directory.
A missing or stale digest, a changed request, an existing destination, a
symlinked request, or any schema violation leaves no final bundle.

Supplying the digest records the data owner's approval of four fixed
statements: privacy, sample fidelity, result fidelity, and usefulness. Approval
is not transferable to another request or another set of artifacts.

After receiving the approved directory, the reviewer validates it independently:

```bash
codeprobe snapshot evidence validate approved-evidence \
  --expect sha256:<digest-from-data-owner> \
  --no-json
```

Validation securely reads only an exact five-file directory, reruns every
schema and cross-artifact binding check, verifies the fixed `findings.md`
rendering, compares the bundle digest in constant time with the final preview
digest received from the data owner through a separate authenticated
channel, and reports only that digest and the bounded conclusion. Extra,
missing, oversized, non-UTF-8, symlinked, modified, or differently bound
artifacts are refused. A digest carried only inside the directory is not proof
of data-owner approval.

## Fixed artifacts

The destination contains exactly these files:

| File and schema | Exact top-level fields |
| --- | --- |
| `run-manifest.json` — `codeprobe.zero-code-access.run-manifest.v1` | `schema_version`, `approval_digest`, `artifact_names`, `codeprobe_version`, `environment`, `configurations`, `run_counts` |
| `sample-attestation.json` — `codeprobe.zero-code-access.sample-attestation.v1` | `schema_version`, `approval_digest`, `window`, `selection_method`, `changed_after_results`, `task_pairs`, `category_counts`, `exclusions`, `attrition_count`, `representative`, `data_owner_attestation` |
| `aggregate-results.json` — `codeprobe.zero-code-access.aggregate-results.v1` | `schema_version`, `approval_digest`, `conclusion`, `evidence_sufficient`, `quality_metric`, `repeats_per_task`, `paired_task_count`, `paired_task_set_same`, `configurations`, `comparison`, `validity_warnings` |
| `findings.md` — `codeprobe.zero-code-access.findings.v1` | Fixed front matter, bounded conclusion, aggregate configuration table, and aggregate comparison |
| `support-log.json` — `codeprobe.zero-code-access.support-log.v1` | `schema_version`, `approval_digest`, `disqualified`, `events` |

Nested objects are also closed:

- Configuration identities contain only `configuration_id` (`A` or `B`) and a
  SHA-256 `configuration_digest`.
- Environment posture contains only the fixed execution location,
  data-owner-only repository access, and an allowlisted network posture.
- Each sample pair contains only SHA-256 task and verifier digests plus an
  anonymous `category_NN` identifier.
- Category counts contain the anonymous category, selected count, and paired
  scorable count. Exclusions contain an allowlisted reason and count.
- Configuration results contain aggregate run counts, mean quality and its
  interval, aggregate cost and coverage, and aggregate latency.
- The comparison contains aggregate differences, uncertainty, comparability,
  refusal code, p-value, effect size, and its declared method.
- A support event contains only a contiguous sequence number and allowlisted
  actor-role and event-kind codes.

All JSON schemas reject missing fields, extra fields, unknown enum values,
malformed digests, non-finite numbers, invalid ranges, count inconsistencies,
and cross-artifact mismatches. Validation errors identify the structural
location but never echo an unexpected field name or value.

## Local request

`request.json` is local input and is not copied into the bundle. Its schema is
`codeprobe.zero-code-access.request.v1`, with exactly these sections:

| Section | Required content |
| --- | --- |
| `run` | CodeProbe version, fixed environment posture, and configurations A then B |
| `sample` | ISO date window, predeclared selection method, digested task pairs, anonymous category counts, exclusions, attrition, representativeness, and whether the sample changed after results |
| `results` | Metric, repeats, paired count and same-set declaration, A/B aggregate results, aggregate comparison, and allowlisted warnings |
| `finding` | `advance_a`, `advance_b`, or `insufficient_evidence` |
| `support` | Sanitized, coded support events only |

Every exported string is a fixed enum, anonymous identifier, the trusted
runtime CodeProbe version, ISO date, or SHA-256 digest. The request's version
must exactly match the runtime; data-owner-supplied version labels are
rejected. There are no free-form description fields. That structural allowlist
is intentional: redacting arbitrary prose after collection would not provide
the same guarantee.

The parser also requires:

- configurations and aggregate results ordered as A then B;
- unique task digests and anonymous categories with reconciled counts;
- scorable runs equal to paired tasks multiplied by repeats;
- nonnegative finite costs and latencies, with cost absent exactly when
  coverage is zero; and
- a regular, non-symlink UTF-8 JSON request no larger than 4 MiB.

## Evidence and independence gates

An advance conclusion is refused unless there are at least 10 paired distinct
tasks, three repeats per task and configuration, identical task sets, an
unchanged representative sample, a comparable report, and no disqualifying
support.

The bundle derives fixed warning codes when a gate fails. The only valid
conclusion in that state is `insufficient_evidence`. Provider Engineering
involvement, provider access to the data-owner environment, bespoke code,
manual evidence repair, prohibited/raw data receipt, or raw-result
reinterpretation disqualifies the comparison.

These checks are structural policy enforcement, not semantic keyword
classification. The data owner decides whether the declared sample is
representative and which bounded conclusion is appropriate.

## Failure behavior

The exporter is deny-by-default. It refuses rather than attempting to sanitize:

- any unknown or prohibited field at any nesting level;
- an identifying value where a digest, anonymous identifier, or enum is
  required;
- a changed approval digest in any artifact or the owner attestation;
- task-level rows, source, paths, prompts, patches, traces, raw results, logs,
  diagnostics, or free-form findings; and
- partial, overwritten, or non-atomic publication.

Keep the local request and full experiment data inside the data-owner
environment. Share only the newly created five-file directory after the data
owner has reviewed the preview. The receiver must run
`codeprobe snapshot evidence validate BUNDLE --expect TRUSTED_DIGEST --no-json`
with the separately authenticated data-owner digest before reviewing its
bounded conclusion.

The complete install-to-export sequence, role boundaries, sampling worksheet,
and participant consent checklist are in the
[zero-code-access operator kit](pilot/zero-code-access/README.md).
