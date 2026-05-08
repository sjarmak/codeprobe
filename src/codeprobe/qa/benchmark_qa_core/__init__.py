"""Schema-agnostic QA library for benchmark task definitions.

Consumed by codeprobe, EnterpriseBench (EB), and CodeScaleBench (CSB) adapters.
Each rig parses its own task-meta schema and feeds already-extracted inputs into
these pure-functional checks. Findings come back as a flat ``list[Finding]``
that the rig surfaces however it likes (CLI report, dolt write, etc.).

Public API:

* :class:`Finding` — uniform per-issue record
* :class:`OracleConstraints` — knobs for ``check_oracle_coherence``
* :func:`check_oracle_coherence` — file/symbol/language/path checks against the
  instruction text
* :func:`check_scoring_honesty` — verifies the declared scoring method matches
  a sanctioned tier
* :func:`check_aux_file_leakage` — flags oracle tokens that appear in
  context/auxiliary files visible to the agent

Finding-code namespace (see individual functions for full meaning):

* ``A*`` — oracle file-existence (``check_oracle_coherence``)
* ``B*`` — oracle symbol-existence (``check_oracle_coherence``)
* ``C*`` — language-match (``check_oracle_coherence``)
* ``D*`` — path-include/exclude (``check_oracle_coherence``)
* ``E*`` — scoring-method-tier (``check_scoring_honesty``)
* ``F*`` — aux-file-leakage (``check_aux_file_leakage``)
"""

from codeprobe.qa.benchmark_qa_core.leakage import check_aux_file_leakage
from codeprobe.qa.benchmark_qa_core.oracle import check_oracle_coherence
from codeprobe.qa.benchmark_qa_core.scoring import check_scoring_honesty
from codeprobe.qa.benchmark_qa_core.types import Finding, OracleConstraints

__all__ = [
    "Finding",
    "OracleConstraints",
    "check_aux_file_leakage",
    "check_oracle_coherence",
    "check_scoring_honesty",
]
