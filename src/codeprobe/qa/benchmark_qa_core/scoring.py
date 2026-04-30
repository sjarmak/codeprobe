"""Scoring honesty checks.

Verifies that a task's declared scoring method maps to one of the sanctioned
tiers the rig has agreed to honour. Tier names are owned by the caller — the
lib only does the lookup.
"""

from __future__ import annotations

from codeprobe.qa.benchmark_qa_core.types import Finding


def check_scoring_honesty(
    task_meta: dict,
    scoring_method_tiers: dict[str, str],
) -> list[Finding]:
    """Verify ``task_meta['scoring_method']`` maps to a sanctioned tier.

    A "sanctioned tier" is anything the rig has explicitly approved (e.g. the
    classic codeprobe split: ``"strict"`` / ``"loose"`` / ``"calibrated"``).
    A scoring method missing from the tier table is the canonical "honesty"
    smell — most often it means the task author wrote a custom method without
    declaring its tier, which silently breaks aggregate scoring.

    Checks performed:

    * **E1** — ``task_meta`` is missing the ``scoring_method`` field entirely.
    * **E2** — ``scoring_method`` is set, but its value is not a key in the
      ``scoring_method_tiers`` map.
    * **E3** — ``scoring_method`` resolves to an empty/falsy tier label,
      indicating the table author flagged it as un-tiered (e.g. ``""`` or
      ``"unknown"`` are common sentinels). We treat any tier value that
      lower-cases to ``"unknown"`` or that is empty after stripping as
      un-tiered.

    Args:
        task_meta: Already-parsed task metadata. Must be a dict; expected to
            contain ``"scoring_method"`` at the top level. Other shapes are
            not coerced — that's the rig adapter's job.
        scoring_method_tiers: Map from scoring-method name (e.g.
            ``"exact_match"``) to tier label (e.g. ``"strict"``). The rig
            supplies this table; the lib does not bake in any methods.

    Returns:
        Flat list of findings, empty when the task's scoring method is
        sanctioned.
    """
    findings: list[Finding] = []

    if "scoring_method" not in task_meta:
        findings.append(
            Finding(
                severity="error",
                code="E1",
                message="task_meta is missing the 'scoring_method' field.",
                location="scoring_method",
                suggested_fix=(
                    "Add a scoring_method field whose value is one of: "
                    + ", ".join(sorted(scoring_method_tiers))
                    if scoring_method_tiers
                    else "Add a scoring_method field."
                ),
            )
        )
        return findings

    method = task_meta["scoring_method"]
    if method not in scoring_method_tiers:
        known = ", ".join(sorted(scoring_method_tiers)) or "(none configured)"
        findings.append(
            Finding(
                severity="error",
                code="E2",
                message=(
                    f"scoring_method {method!r} is not in the sanctioned tier "
                    f"table. Known methods: {known}"
                ),
                location="scoring_method",
                suggested_fix=(
                    "Either declare a tier for this method in "
                    "scoring_method_tiers, or switch to a known method."
                ),
            )
        )
        return findings

    tier = scoring_method_tiers[method]
    if not tier or not str(tier).strip() or str(tier).strip().lower() == "unknown":
        findings.append(
            Finding(
                severity="warning",
                code="E3",
                message=(
                    f"scoring_method {method!r} resolves to an un-tiered "
                    f"label: {tier!r}"
                ),
                location="scoring_method",
                suggested_fix=(
                    "Promote this method to a real tier (e.g. strict / "
                    "loose / calibrated) before using it in aggregates."
                ),
            )
        )

    return findings
