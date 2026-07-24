"""Security tests for oracle curator candidate path containment."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

from codeprobe.mining import oracle_curator
from codeprobe.mining.consensus import BackendResult
from codeprobe.mining.oracle_curator import curate_consensus


class TestReadSnippetPathContainment:
    """Backend-supplied candidate paths are untrusted (codeprobe-opp7)."""

    @staticmethod
    def _assert_post_validation_swap_rejected(
        *,
        repo: Path,
        candidate_path: str,
        swap: Callable[[], None],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        candidate = repo / candidate_path
        resolve_path = oracle_curator._resolve_candidate_path

        def resolve_then_swap(
            path: Path,
            allowed_roots: tuple[Path, ...],
            rel_path: str,
        ) -> oracle_curator._CandidateResolution | None:
            resolved = resolve_path(path, allowed_roots, rel_path)
            if path == candidate:
                swap()
            return resolved

        with (
            patch.object(
                oracle_curator,
                "_resolve_candidate_path",
                side_effect=resolve_then_swap,
            ),
            patch.object(
                oracle_curator,
                "call_claude",
            ) as mock_call,
        ):
            vote = oracle_curator._curate_with_llm(
                symbol="Foo",
                defining_file="src/foo.py",
                candidate_path=candidate_path,
                found_by="grep",
                repo_paths=[repo],
                timeout_seconds=30,
            )

        assert vote.keep is False
        assert vote.llm_called is False
        assert vote.error is not None
        assert "unsafe candidate path" in vote.error
        assert "TOP SECRET" not in caplog.text
        mock_call.assert_not_called()

    def test_rejects_traversal_escape(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("TOP SECRET")

        with pytest.raises(oracle_curator.UnsafeCandidatePathError):
            oracle_curator._read_snippet([repo], "../secret.txt")

    def test_rejects_absolute_path(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("TOP SECRET")

        with pytest.raises(oracle_curator.UnsafeCandidatePathError):
            oracle_curator._read_snippet([repo], str(secret))

    def test_rejects_absolute_path_with_no_roots(self) -> None:
        with pytest.raises(oracle_curator.UnsafeCandidatePathError):
            oracle_curator._read_snippet([], "/etc/passwd")

    def test_rejects_symlink_escape(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("TOP SECRET")
        (repo / "leak.py").symlink_to(secret)

        with pytest.raises(oracle_curator.UnsafeCandidatePathError):
            oracle_curator._read_snippet([repo], "leak.py")

    def test_rejection_message_excludes_file_bytes(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (tmp_path / "secret.txt").write_text("TOP SECRET")

        with pytest.raises(
            oracle_curator.UnsafeCandidatePathError
        ) as excinfo:
            oracle_curator._read_snippet([repo], "../secret.txt")
        assert "TOP SECRET" not in str(excinfo.value)

    def test_escape_from_one_root_still_reads_from_another(
        self, tmp_path: Path
    ) -> None:
        # ``../two/b.py`` escapes ``one`` but lands inside ``tmp_path``,
        # under the configured ``two`` root — that is a legitimate read.
        first = tmp_path / "one"
        first.mkdir()
        second = tmp_path / "two"
        second.mkdir()
        (second / "b.py").write_text("from src.foo import Foo\n")

        snippet = oracle_curator._read_snippet(
            [first, second], "../two/b.py"
        )
        assert "import Foo" in snippet

    def test_escape_from_every_root_is_rejected(
        self, tmp_path: Path
    ) -> None:
        repos = tmp_path / "repos"
        first = repos / "one"
        second = repos / "two"
        first.mkdir(parents=True)
        second.mkdir()
        (tmp_path / "secret.txt").write_text("TOP SECRET")

        with pytest.raises(oracle_curator.UnsafeCandidatePathError):
            oracle_curator._read_snippet(
                [first, second], "../../secret.txt"
            )

    def test_escape_is_not_masked_by_missing_path_in_another_root(
        self, tmp_path: Path
    ) -> None:
        first = tmp_path / "one"
        second = tmp_path / "two"
        first.mkdir()
        second.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_text("TOP SECRET")
        (first / "candidate.py").symlink_to(secret)

        with patch.object(oracle_curator, "call_claude") as mock_call:
            vote = oracle_curator._curate_with_llm(
                symbol="Foo",
                defining_file="src/foo.py",
                candidate_path="candidate.py",
                found_by="grep",
                repo_paths=[first, second],
                timeout_seconds=30,
            )

        assert vote.keep is False
        assert vote.llm_called is False
        assert vote.error is not None
        assert "unsafe candidate path" in vote.error
        assert "TOP SECRET" not in vote.error
        mock_call.assert_not_called()

    def test_symlink_target_inside_another_root_is_read(
        self, tmp_path: Path
    ) -> None:
        first = tmp_path / "one"
        second = tmp_path / "two"
        first.mkdir()
        second.mkdir()
        target = second / "actual.py"
        target.write_text("from src.foo import Foo\n")
        (first / "candidate.py").symlink_to(target)

        snippet = oracle_curator._read_snippet(
            [first, second], "candidate.py"
        )

        assert "import Foo" in snippet

    def test_symlink_loop_returns_error_without_calling_model(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "loop.py").symlink_to("loop.py")

        with patch.object(oracle_curator, "call_claude") as mock_call:
            vote = oracle_curator._curate_with_llm(
                symbol="Foo",
                defining_file="src/foo.py",
                candidate_path="loop.py",
                found_by="grep",
                repo_paths=[repo],
                timeout_seconds=30,
            )

        assert vote.keep is False
        assert vote.llm_called is False
        assert vote.error is not None
        assert "unsafe candidate path" in vote.error
        mock_call.assert_not_called()

    def test_nul_path_returns_error_without_calling_model(
        self, tmp_path: Path
    ) -> None:
        with patch.object(oracle_curator, "call_claude") as mock_call:
            vote = oracle_curator._curate_with_llm(
                symbol="Foo",
                defining_file="src/foo.py",
                candidate_path="bad\0.py",
                found_by="grep",
                repo_paths=[tmp_path],
                timeout_seconds=30,
            )

        assert vote.keep is False
        assert vote.llm_called is False
        assert vote.error is not None
        assert "unsafe candidate path" in vote.error
        mock_call.assert_not_called()

    def test_regular_file_replacement_never_reaches_model(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        candidate = repo / "candidate.py"
        candidate.write_text("from src.foo import Foo\n")
        secret = tmp_path / "secret.txt"
        secret.write_text("TOP SECRET")

        def replace_candidate() -> None:
            candidate.unlink()
            secret.rename(candidate)

        self._assert_post_validation_swap_rejected(
            repo=repo,
            candidate_path="candidate.py",
            swap=replace_candidate,
            caplog=caplog,
        )

    def test_directory_replacement_never_reaches_model(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = tmp_path / "repo"
        nested = repo / "nested"
        nested.mkdir(parents=True)
        (nested / "candidate.py").write_text("safe content")
        attacker = tmp_path / "attacker"
        attacker.mkdir()
        (attacker / "candidate.py").write_text("TOP SECRET")

        def replace_directory() -> None:
            nested.rename(tmp_path / "validated-nested")
            attacker.rename(nested)

        self._assert_post_validation_swap_rejected(
            repo=repo,
            candidate_path="nested/candidate.py",
            swap=replace_directory,
            caplog=caplog,
        )

    def test_root_replacement_never_reaches_model(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "candidate.py").write_text("safe content")
        attacker = tmp_path / "attacker"
        attacker.mkdir()
        (attacker / "candidate.py").write_text("TOP SECRET")

        def replace_root() -> None:
            repo.rename(tmp_path / "validated-repo")
            attacker.rename(repo)

        self._assert_post_validation_swap_rejected(
            repo=repo,
            candidate_path="candidate.py",
            swap=replace_root,
            caplog=caplog,
        )

    def test_swap_to_external_symlink_never_reaches_model(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        candidate = repo / "candidate.py"
        candidate.write_text("from src.foo import Foo\n")
        secret = tmp_path / "secret.txt"
        secret.write_text("TOP SECRET")

        def replace_with_symlink() -> None:
            candidate.unlink()
            candidate.symlink_to(secret)

        self._assert_post_validation_swap_rejected(
            repo=repo,
            candidate_path="candidate.py",
            swap=replace_with_symlink,
            caplog=caplog,
        )

    def test_non_regular_candidate_never_reaches_model(
        self, tmp_path: Path
    ) -> None:
        candidate = tmp_path / "candidate"
        candidate.mkdir()

        with patch.object(oracle_curator, "call_claude") as mock_call:
            vote = oracle_curator._curate_with_llm(
                symbol="Foo",
                defining_file="src/foo.py",
                candidate_path="candidate",
                found_by="grep",
                repo_paths=[tmp_path],
                timeout_seconds=30,
            )

        assert vote.keep is False
        assert vote.llm_called is False
        assert vote.error is not None
        assert "not a regular file" in vote.error
        mock_call.assert_not_called()

    def test_descriptor_open_error_never_reaches_model(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "candidate.py").write_text("safe content")

        with (
            patch.object(
                oracle_curator.os,
                "open",
                side_effect=PermissionError("blocked"),
            ),
            patch.object(oracle_curator, "call_claude") as mock_call,
        ):
            vote = oracle_curator._curate_with_llm(
                symbol="Foo",
                defining_file="src/foo.py",
                candidate_path="candidate.py",
                found_by="grep",
                repo_paths=[tmp_path],
                timeout_seconds=30,
            )

        assert vote.keep is False
        assert vote.llm_called is False
        assert vote.error is not None
        assert "cannot be opened safely" in vote.error
        mock_call.assert_not_called()

    def test_unsafe_tier2_candidate_is_quarantined(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (tmp_path / "secret.txt").write_text("TOP SECRET")
        results = [
            BackendResult(
                backend="grep",
                files=frozenset({"a.py", "../secret.txt"}),
                available=True,
            ),
            BackendResult(
                backend="ast",
                files=frozenset({"a.py"}),
                available=True,
            ),
        ]

        with patch.object(
            oracle_curator, "llm_available", return_value=True
        ), patch.object(oracle_curator, "call_claude") as mock_call:
            out = curate_consensus(
                backend_results=results,
                symbol="Foo",
                defining_file="src/foo.py",
                repo_paths=[repo],
                use_llm=True,
            )

        # No prompt was ever built for the escaping candidate.
        mock_call.assert_not_called()
        assert {it.path for it in out.items} == {"a.py"}
        reasons = {p: r for p, r in out.quarantined}
        assert "../secret.txt" in reasons
        assert reasons["../secret.txt"].startswith("unsafe candidate path:")
        assert "TOP SECRET" not in reasons["../secret.txt"]
        assert out.llm_used is False

    def test_llm_curator_returns_non_content_error_without_calling_model(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (tmp_path / "secret.txt").write_text("TOP SECRET")

        with patch.object(oracle_curator, "call_claude") as mock_call:
            vote = oracle_curator._curate_with_llm(
                symbol="Foo",
                defining_file="src/foo.py",
                candidate_path="../secret.txt",
                found_by="grep",
                repo_paths=[repo],
                timeout_seconds=30,
            )

        assert vote.keep is False
        assert vote.error is not None
        assert "unsafe candidate path" in vote.error
        assert "TOP SECRET" not in vote.error
        assert vote.llm_called is False
        mock_call.assert_not_called()
