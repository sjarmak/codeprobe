"""Task family definitions for org-scale mining.

Each family defines structural patterns to scan for and question templates
for both single-hop (grep-equivalent) and multi-hop (reasoning-required)
task variants.

ZFC compliant: patterns are structural (globs + regex). The LLM generates
questions from scan results but never touches ground truth.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TaskFamily:
    """A category of org-scale comprehension task.

    Attributes:
        name: Family identifier matching ORG_SCALE_CATEGORIES.
        description: Human-readable description.
        glob_patterns: File extensions to scan (e.g., ``("**/*.go", "**/*.py")``).
        content_patterns: Regex patterns to match in file content.
        oracle_type: Expected answer type: ``"file_list"`` | ``"count"`` | ``"boolean"``.
        min_hits: Minimum pattern matches to produce a task.
        max_hits: Cap results to avoid blowing up on large repos.
        multi_hop: Whether this family has multi-hop variants.
        multi_hop_description: Description of what multi-hop requires.
    """

    name: str
    description: str
    glob_patterns: tuple[str, ...]
    content_patterns: tuple[str, ...]
    oracle_type: str = "file_list"
    min_hits: int = 3
    max_hits: int = 200
    multi_hop: bool = True
    multi_hop_description: str = ""


# ---------------------------------------------------------------------------
# Phase 1 families (3 families, single-repo, with multi-hop variants)
# ---------------------------------------------------------------------------

MIGRATION_INVENTORY = TaskFamily(
    name="migration-inventory",
    description="Find files containing deprecated API annotations or markers.",
    glob_patterns=(
        "**/*.py",
        "**/*.go",
        "**/*.java",
        "**/*.ts",
        "**/*.js",
        "**/*.rs",
        "**/*.kt",
        "**/*.cpp",
        "**/*.c",
        "**/*.h",
        "**/*.rb",
    ),
    content_patterns=(
        r"@[Dd]eprecated",
        r"#\[deprecated",
        r"//\s*Deprecated:",
        r"warnings\.warn\(.*[Dd]eprecat",
        r"\.warn\(.*[Dd]eprecat",
        r"@deprecated",
    ),
    min_hits=3,
    multi_hop=True,
    multi_hop_description=(
        "Find callers of deprecated symbols — requires tracing call sites, "
        "not just finding annotations."
    ),
)

COMPLIANCE_AUDIT = TaskFamily(
    name="compliance-audit",
    description="Find files matching security and configuration patterns.",
    glob_patterns=(
        "**/*.go",
        "**/*.py",
        "**/*.java",
        "**/*.ts",
        "**/*.js",
        "**/*.yaml",
        "**/*.yml",
        "**/*.toml",
        "**/*.json",
        "**/*.rs",
        "**/*.rb",
    ),
    content_patterns=(
        r"tls\.Config",
        r"TLSConfig",
        r"ssl_context",
        r"SSLContext",
        r"crypto/tls",
        r"InsecureSkipVerify",
        r"MinVersion.*tls\.",
        r"certificate",
    ),
    min_hits=3,
    multi_hop=True,
    multi_hop_description=(
        "Determine whether TLS configurations meet a minimum version "
        "requirement — requires reading config values, not just finding files."
    ),
)

CROSS_REPO_DEP_TRACE = TaskFamily(
    name="cross-repo-dep-trace",
    description="Find files importing a specific package or module.",
    glob_patterns=(
        "**/*.go",
        "**/*.py",
        "**/*.java",
        "**/*.ts",
        "**/*.js",
        "**/*.rs",
    ),
    content_patterns=(
        # These are templates — the scanner substitutes the actual package name.
        # At scan time, the scanner discovers top imported packages and uses
        # those as the patterns.
        r'^import\s+"',
        r"^from\s+\S+\s+import",
        r"^import\s+\S+",
        r'require\s*\(\s*["\']',
    ),
    min_hits=5,
    multi_hop=True,
    multi_hop_description=(
        "Find files that import package X AND re-export or extend its types "
        "— requires understanding export patterns, not just import grep."
    ),
)


# All Phase 1 families
FAMILIES: tuple[TaskFamily, ...] = (
    MIGRATION_INVENTORY,
    COMPLIANCE_AUDIT,
    CROSS_REPO_DEP_TRACE,
)

FAMILY_BY_NAME: dict[str, TaskFamily] = {f.name: f for f in FAMILIES}
