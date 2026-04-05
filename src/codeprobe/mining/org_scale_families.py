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


# ---------------------------------------------------------------------------
# Phase 2 families (3 families, single-repo, with multi-hop variants)
# ---------------------------------------------------------------------------

INCIDENT_DEBUG = TaskFamily(
    name="incident-debug",
    description="Find files containing error types and exception handling patterns.",
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
        r"class\s+\w+Error",
        r"type\s+\w+Error\s+struct",
        r"panic\(",
        r"raise\s+\w+Error",
        r"throw\s+new\s+\w+Error",
    ),
    min_hits=3,
    multi_hop=True,
    multi_hop_description=(
        "Trace error propagation across call chains — requires following "
        "raise/catch paths through multiple files, not just finding error types."
    ),
)

PLATFORM_KNOWLEDGE = TaskFamily(
    name="platform-knowledge",
    description="Find files containing plugin, extension, and registry patterns.",
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
        r"Register\w*\(",
        r"\.register\(",
        r"Plugin",
        r"extension_point",
        r"Hook\w*\(",
        r"Factory\w*\(",
    ),
    min_hits=3,
    multi_hop=True,
    multi_hop_description=(
        "Map which plugins hook into which extension points — requires tracing "
        "registration calls to their consuming dispatch sites, not just finding "
        "registration patterns."
    ),
)

CROSS_REPO_CONFIG_TRACE = TaskFamily(
    name="cross-repo-config-trace",
    description="Find files containing configuration struct and access patterns.",
    glob_patterns=(
        "**/*.go",
        "**/*.py",
        "**/*.java",
        "**/*.ts",
        "**/*.js",
        "**/*.rs",
        "**/*.kt",
        "**/*.yaml",
        "**/*.yml",
        "**/*.toml",
        "**/*.json",
        "**/*.rb",
    ),
    content_patterns=(
        r"type\s+\w*Config\s+struct",
        r"class\s+\w*Config",
        r"viper\.\w+",
        r"os\.environ",
        r"envconfig\.",
        r"@ConfigurationProperties",
    ),
    min_hits=3,
    multi_hop=True,
    multi_hop_description=(
        "Trace a config key from definition to consumption — requires following "
        "config structs through parsing, validation, and usage sites, not just "
        "finding config definitions."
    ),
)


# ---------------------------------------------------------------------------
# MCP-advantaged families (tasks where Sourcegraph code intelligence
# outperforms local grep — aliased references, type hierarchies, blast radius)
# ---------------------------------------------------------------------------

SYMBOL_REFERENCE_TRACE = TaskFamily(
    name="symbol-reference-trace",
    description=(
        "Find all files referencing a public symbol, including through "
        "aliased imports, re-exports, and wildcard imports."
    ),
    glob_patterns=(
        "**/*.py",
        "**/*.go",
        "**/*.java",
        "**/*.ts",
        "**/*.js",
        "**/*.rs",
    ),
    content_patterns=(
        r"\bdef\s+\w{6,}\s*\(",
        r"\bclass\s+\w{6,}\s*[:\(]",
        r"\bfunc\s+\w{6,}\s*\(",
    ),
    oracle_type="file_list",
    min_hits=10,
    max_hits=300,
    multi_hop=True,
    multi_hop_description=(
        "Trace symbol references through aliased imports, re-exports, and "
        "wildcard imports — requires compiler-accurate reference resolution."
    ),
)

TYPE_HIERARCHY_CONSUMERS = TaskFamily(
    name="type-hierarchy-consumers",
    description=(
        "Find implementations of a base class or Protocol and all files "
        "that instantiate or call those implementations."
    ),
    glob_patterns=(
        "**/*.py",
        "**/*.go",
        "**/*.java",
        "**/*.ts",
        "**/*.js",
        "**/*.rs",
    ),
    content_patterns=(
        r"class\s+\w+\(.*(?:ABC|Base|Protocol|Interface)\w*",
        r"class\s+\w+\(.*metaclass=ABCMeta",
        r"@abstractmethod",
        r"typing\.Protocol",
        r"\binterface\s+\w+",
    ),
    oracle_type="file_list",
    min_hits=5,
    max_hits=200,
    multi_hop=True,
    multi_hop_description=(
        "Find concrete implementations AND their usage sites — requires "
        "tracing type hierarchy, not just text matching base class name."
    ),
)

CHANGE_SCOPE_AUDIT = TaskFamily(
    name="change-scope-audit",
    description=(
        "Find the blast radius of a recently changed symbol — all files "
        "that depend on it and would need review."
    ),
    glob_patterns=(
        "**/*.py",
        "**/*.go",
        "**/*.java",
        "**/*.ts",
        "**/*.js",
        "**/*.rs",
    ),
    content_patterns=(
        r"\bdef\s+\w+\s*\(",
        r"\bclass\s+\w+",
        r"\bfunc\s+\w+\s*\(",
    ),
    oracle_type="file_list",
    min_hits=5,
    max_hits=300,
    multi_hop=True,
    multi_hop_description=(
        "Combine diff analysis with reference tracing to find all files "
        "affected by a specific code change."
    ),
)


# All families (Phase 1 + Phase 2)
FAMILIES: tuple[TaskFamily, ...] = (
    MIGRATION_INVENTORY,
    COMPLIANCE_AUDIT,
    CROSS_REPO_DEP_TRACE,
    INCIDENT_DEBUG,
    PLATFORM_KNOWLEDGE,
    CROSS_REPO_CONFIG_TRACE,
)

# MCP-advantaged families (separate tuple — only used when mining for
# Sourcegraph MCP comparison experiments)
MCP_FAMILIES: tuple[TaskFamily, ...] = (
    SYMBOL_REFERENCE_TRACE,
    TYPE_HIERARCHY_CONSUMERS,
    CHANGE_SCOPE_AUDIT,
)

ALL_FAMILIES: tuple[TaskFamily, ...] = FAMILIES + MCP_FAMILIES

FAMILY_BY_NAME: dict[str, TaskFamily] = {f.name: f for f in ALL_FAMILIES}
