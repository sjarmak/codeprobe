"""Tests for public API promotion in :mod:`codeprobe.mining.extractor`.

``_get_changed_files`` was previously a private helper but was imported
across module boundaries by :mod:`codeprobe.mining.multi_repo`. It is now
exposed as the public :func:`get_changed_files`; the legacy underscore
name is retained as a deprecated alias so stray internal callers and any
external consumers keep working for one release cycle.
"""

from __future__ import annotations

from codeprobe.mining import extractor


def test_get_changed_files_is_public_callable() -> None:
    assert callable(extractor.get_changed_files)


def test_legacy_alias_is_same_object() -> None:
    """The deprecated alias must still resolve to the public function so
    internal callers that haven't migrated yet keep the exact same
    behaviour (identity check, not just equality).
    """
    assert extractor._get_changed_files is extractor.get_changed_files


def test_multi_repo_imports_public_name() -> None:
    """Cross-module consumers migrated to the public name."""
    from codeprobe.mining import multi_repo

    # multi_repo.py should expose the public symbol, not the underscore.
    assert hasattr(multi_repo, "get_changed_files")
    assert multi_repo.get_changed_files is extractor.get_changed_files
