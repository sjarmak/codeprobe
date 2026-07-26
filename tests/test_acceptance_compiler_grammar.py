"""Grammar and injection-safety tests for the acceptance_compiler token
substitution (CWE-78).

Split out of ``test_acceptance_compiler.py`` to keep each module focused. Covers
the accepted single-line, unquoted, expansion-free shell-word grammar and the
executable repo/workspace/sync injection regressions. The ``_compile_one`` helper
is the shared compile step every case here leans on.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from acceptance.loader import Criterion
from codeprobe.acceptance_compiler import compile_actions

# Fake paths for pure (non-executing) compilation checks.
TARGET_REPO = Path("/fake/target/repo")
WORKSPACE = Path("/fake/workspace")
PROJECT_ROOT = Path("/fake/project")


def _criterion(
    id: str = "G-001",
    check_type: str = "stream_separation",
    tier: str = "behavioral",
    params: dict | None = None,
) -> Criterion:
    return Criterion(
        id=id,
        description="grammar test criterion",
        tier=tier,
        check_type=check_type,
        severity="high",
        prd_source="docs/prd/test.md",
        depends_on=(),
        params=params or {},
    )


def _compile_one(
    command: str,
    *,
    target_repo: Path = TARGET_REPO,
    workspace: Path = WORKSPACE,
    project_root: Path = PROJECT_ROOT,
    cid: str = "G-001",
) -> str:
    """Compile a single stream_separation command template; return its snippet."""
    c = _criterion(id=cid, params={"command": command})
    actions = compile_actions(
        [c], target_repo=target_repo, workspace=workspace, project_root=project_root
    )
    assert len(actions) == 1
    return actions[0].shell_snippet


def _expect_reject(command: str, match: str) -> None:
    """Assert a token-bearing template is rejected at compile time."""
    with pytest.raises(ValueError, match=match):
        _compile_one(
            command,
            target_repo=Path("/r"),
            workspace=Path("/w"),
            project_root=Path("/p"),
        )


class TestTokenGrammar:
    def test_no_token_template_passes_dollar_constructs_through(self) -> None:
        """A template with NO path token may use any shell construct — the
        reparse/expansion guards apply only to token-bearing templates, so
        ${VAR}, $(...) and backticks pass through unchanged."""
        cmd = 'echo "${HOME}" && echo $(date) && echo `whoami`'
        c = _criterion(
            id="NOTOK-001",
            check_type="cli_exit_code",
            params={"command": cmd, "expected_exit": 0},
        )
        actions = compile_actions(
            [c],
            target_repo=TARGET_REPO,
            workspace=WORKSPACE,
            project_root=PROJECT_ROOT,
        )
        assert len(actions) == 1
        assert cmd in actions[0].shell_snippet  # passed through verbatim

    def test_plain_dollar_var_with_token_is_allowed(self) -> None:
        """Plain ``$VAR`` (no ${}, $(), $[] scope) does not reparse a token
        value, so it may coexist with a token."""
        c = _criterion(
            id="PVAR-001",
            check_type="cli_exit_code",
            params={"command": "codeprobe mine {repo} --tenant $CODEPROBE_TENANT"},
        )
        actions = compile_actions(
            [c],
            target_repo=TARGET_REPO,
            workspace=WORKSPACE,
            project_root=PROJECT_ROOT,
        )
        assert len(actions) == 1
        assert str(TARGET_REPO) in actions[0].shell_snippet
        assert "$CODEPROBE_TENANT" in actions[0].shell_snippet

    def test_non_string_params_not_substituted(self) -> None:
        """Integer params like min_count should pass through without crash."""
        c = _criterion(
            id="INT-001",
            check_type="count_ge",
            tier="statistical",
            params={
                "source": "{repo}/.codeprobe/tasks",
                "pattern": "task-*",
                "min_count": 3,
            },
        )
        # Should not raise
        actions = compile_actions(
            [c],
            target_repo=TARGET_REPO,
            workspace=WORKSPACE,
            project_root=PROJECT_ROOT,
        )
        assert len(actions) == 1

    def test_path_tokens_do_not_execute_shell_metacharacters(
        self, tmp_path: Path
    ) -> None:
        """CWE-78: a path token whose value embeds ``$(...)`` must be quoted so
        the substitution is inert — no command executes, and the path is passed
        through literally.

        ``${IFS}`` stands in for the argument separator so the injected
        ``touch${IFS}INJECTION_PROOF`` is a single filename (no literal space in
        the directory name). The command runs with ``cwd=tmp_path``, so a fired
        injection would drop the sentinel there.
        """
        workspace = tmp_path / "ws"  # clean workspace — isolates the token vector
        workspace.mkdir()
        evil_repo = tmp_path / "repo$(touch${IFS}INJECTION_PROOF)"
        evil_repo.mkdir()
        sentinel = tmp_path / "INJECTION_PROOF"
        c = _criterion(
            id="INJ-001",
            check_type="stream_separation",
            params={"command": "echo {repo}"},
        )
        actions = compile_actions(
            [c],
            target_repo=evil_repo,
            workspace=workspace,
            project_root=tmp_path,
        )
        subprocess.run(
            ["bash", "-c", actions[0].shell_snippet], cwd=tmp_path, check=False
        )
        assert not sentinel.exists(), "command substitution in {repo} executed!"
        stdout = (workspace / "INJ-001.stdout").read_text()
        assert str(evil_repo) in stdout  # literal path passed through intact

    def test_workspace_path_with_metacharacters_does_not_execute(
        self, tmp_path: Path
    ) -> None:
        """CWE-78 in the EMITTERS: even when the workspace directory name embeds
        ``$(...)``, the generated redirections/paths must not execute it. Double
        quotes do not stop command substitution — every generated path must be
        shell-quoted. Artifacts must still land in the (weirdly named) workspace.
        """
        sentinel = tmp_path / "WS_PWNED"
        workspace = tmp_path / "ws$(touch${IFS}WS_PWNED)"
        workspace.mkdir()
        c = _criterion(
            id="WSINJ-001",
            check_type="stream_separation",
            params={"command": "echo hello"},
        )
        actions = compile_actions(
            [c],
            target_repo=tmp_path,
            workspace=workspace,
            project_root=tmp_path,
        )
        subprocess.run(
            ["bash", "-c", actions[0].shell_snippet], cwd=tmp_path, check=False
        )
        assert not sentinel.exists(), "workspace-path command substitution executed!"
        assert (workspace / "WSINJ-001.stdout").read_text().strip() == "hello"
        assert (workspace / "WSINJ-001.exit").read_text().strip() == "0"

    def test_sync_emitter_quotes_workspace_and_target_paths(
        self, tmp_path: Path
    ) -> None:
        """The sync/count emitter (mkdir/cp) must quote its paths too — a
        workspace name with ``$(...)`` must not execute during the sync."""
        sentinel = tmp_path / "SYNC_PWNED"
        workspace = tmp_path / "ws$(touch${IFS}SYNC_PWNED)"
        workspace.mkdir()
        target = tmp_path / "target"
        (target / ".codeprobe" / "tasks" / "t1").mkdir(parents=True)
        (target / ".codeprobe" / "tasks" / "t1" / "instruction.md").write_text("x")
        c = _criterion(
            id="SYNC-001",
            check_type="count_ge",
            tier="statistical",
            params={
                "source": "{repo}/.codeprobe/tasks",
                "pattern": "*/instruction.md",
                "min_count": 1,
            },
        )
        actions = compile_actions(
            [c], target_repo=target, workspace=workspace, project_root=tmp_path
        )
        subprocess.run(
            ["bash", "-c", actions[0].shell_snippet], cwd=tmp_path, check=False
        )
        assert not sentinel.exists(), "sync-emitter command substitution executed!"
        # The sync actually copied .codeprobe into the weird-named workspace.
        assert (workspace / ".codeprobe" / "tasks" / "t1" / "instruction.md").exists()

    def test_workspace_token_suffix_concatenation_survives_spaces(
        self, tmp_path: Path
    ) -> None:
        """``{workspace}/suffix`` must resolve correctly even when the workspace
        path contains a space (the quote wraps the path, the suffix concatenates)."""
        workspace = tmp_path / "work space"
        workspace.mkdir()
        c = _criterion(
            id="WS-SUFFIX-001",
            check_type="stream_separation",
            params={"command": "echo {workspace}/probes"},
        )
        actions = compile_actions(
            [c],
            target_repo=tmp_path,
            workspace=workspace,
            project_root=tmp_path,
        )
        subprocess.run(
            ["bash", "-c", actions[0].shell_snippet], cwd=workspace, check=False
        )
        stdout = (workspace / "WS-SUFFIX-001.stdout").read_text().strip()
        assert stdout == f"{workspace}/probes"

    def test_token_literal_inside_a_path_value_is_not_rescanned(self) -> None:
        """A path VALUE that itself contains another token literal (e.g. a repo
        path containing ``{workspace}``) must survive intact. Sequential
        ``.replace()`` rescans inserted values and corrupts it; substitution
        must be a single pass."""
        snippet = _compile_one(
            "echo {repo}",
            target_repo=Path("/repo/{workspace}/x"),  # literal {workspace}
            workspace=Path("/real/ws"),
            project_root=Path("/p"),
        )
        # The literal token in the repo path survived verbatim (not rescanned
        # into the workspace path).
        assert "/repo/{workspace}/x" in snippet
        assert "/repo//real/ws/x" not in snippet  # the corruption signature

    @pytest.mark.parametrize("template", ['echo "{repo}"', "echo '{repo}'"])
    def test_token_inside_shell_quotes_is_rejected(self, template: str) -> None:
        """A token wrapped in shell quotes defeats shlex.quote (``"$(...)"``
        still expands). The conservative grammar rejects it: the token is
        preceded by a quote char, not whitespace, so it does not begin a word."""
        _expect_reject(template, "shell-word")

    @pytest.mark.parametrize(
        "command",
        [
            'echo " {repo} "',  # space-bounded, inside double quotes
            "echo ' {repo} '",  # space-bounded, inside single quotes
            'echo "\t{repo}\t"',  # tab-bounded, inside double quotes
        ],
    )
    def test_whitespace_bounded_token_inside_quotes_is_rejected(
        self, command: str
    ) -> None:
        """A token surrounded by whitespace but INSIDE a quote context passes
        the boundary checks yet is still quoted (defeating shlex.quote), so
        quote-state tracking must reject it independently of adjacency."""
        _expect_reject(command, "shell-word|quoted context")

    def test_token_after_escaped_quote_stays_rejected(self) -> None:
        """A backslash-escaped quote inside a double-quoted string does NOT
        close the quote context (shell rule), so a token after it is still
        inside a word. The conservative grammar rejects it: the token is
        preceded by ``"`` (not whitespace), so it does not begin a word."""
        # actual command: printf %s "prefix\"{repo}"
        _expect_reject('printf %s "prefix\\"{repo}"', "shell-word")

    def test_ordinary_backslash_text_does_not_block_a_bare_token(self) -> None:
        """Ordinary backslash text (``\\n`` inside single quotes) must not break
        substitution of a following bare token."""
        # actual command: printf '%s\n' {repo}
        snippet = _compile_one(
            "printf '%s\\n' {repo}",
            target_repo=Path("/repo-xyz"),
            workspace=Path("/w"),
            project_root=Path("/p"),
        )
        assert "/repo-xyz" in snippet  # {repo} substituted
        assert "{repo}" not in snippet  # token consumed, not left literal

    @pytest.mark.parametrize(
        "command",
        [
            "echo $(( 1 + 1 )) {repo}",  # arithmetic $((
            "echo $(echo {repo})",  # command substitution — token nested in $()
            "echo `echo {repo}`",  # backtick command substitution
            "echo ${HOME} {repo}",  # parameter expansion ${
            "echo $[1+1] {repo}",  # legacy arithmetic $[
        ],
    )
    def test_reparsing_or_expansion_scope_with_token_is_rejected(
        self, command: str
    ) -> None:
        """A token-bearing template must stay genuinely flat: flat quote-state
        cannot model a token nested in ``$()`` / backticks / ``${…}`` / ``$[…]``,
        so every such scope is rejected outright — a value must never share a
        template with a construct that could reparse or nest it. Plain ``$VAR``
        (no scope) is fine and covered by a passthrough positive."""
        _expect_reject(command, "substitution|expansion")

    @pytest.mark.parametrize("command", ["echo x{repo}", "echo {repo}x", "echo a={repo}"])
    def test_token_not_a_standalone_word_is_rejected(self, command: str) -> None:
        """A token that does not begin a shell-word (adjacent to other text) or
        that ends at something other than a word boundary / ``/suffix`` is
        rejected."""
        _expect_reject(command, "shell-word|word boundary")

    @pytest.mark.parametrize(
        "command",
        [
            "codeprobe validate {tasks_dir}",
            "codeprobe interpret {repo} --format json",
            "codeprobe probe {repo} --count 5 --output {workspace}/log-stderr-probes",
            "codeprobe mine {repo} && test -f {repo}/.codeprobe/experiment.json",
        ],
    )
    def test_accepted_grammar_forms_compile(self, command: str) -> None:
        """The accepted grammar (bare token, token mid-command, ``{token}/suffix``,
        compound with ``&&``) — the shapes the real manifest uses — compiles."""
        snippet = _compile_one(command)
        for tok in ("{repo}", "{workspace}", "{tasks_dir}"):
            assert tok not in snippet

    def test_multiline_template_with_token_is_rejected(self, tmp_path: Path) -> None:
        """A path token in a multiline template (e.g. a heredoc body) is not a
        shell word: shlex.quote would become literal text while heredoc
        expansion still runs ``$(...)``. Such templates are rejected at compile
        time, so the dangerous command is never built or executed."""
        sentinel = tmp_path / "HEREDOC_PWNED"
        evil = tmp_path / "r$(touch${IFS}HEREDOC_PWNED)"
        evil.mkdir()
        template = "cat <<EOF\n{repo}\nEOF\n"  # token inside a heredoc body
        c = _criterion(
            id="HD-001",
            check_type="stream_separation",
            params={"command": template},
        )
        with pytest.raises(ValueError, match="single-line|multiline|heredoc"):
            compile_actions(
                [c],
                target_repo=evil,
                workspace=tmp_path / "w",
                project_root=tmp_path,
            )
        assert not sentinel.exists()  # rejected before any execution could occur

    def test_multiline_template_without_token_is_allowed(self) -> None:
        """A multiline template that uses NO path token is still fine — the
        rejection is narrowly scoped to token-bearing multiline commands."""
        assert "echo one\necho two" in _compile_one("echo one\necho two")

    def test_fixture_param_rejects_sibling_prefix_escape(self, tmp_path: Path) -> None:
        """A fixture resolving to a sibling directory that merely shares a name
        prefix (``project`` vs ``project-evil``) must be rejected. A naive
        string ``startswith`` check accepts it; containment must be path-aware."""
        project = tmp_path / "project"
        project.mkdir()
        (tmp_path / "project-evil").mkdir()
        c = _criterion(
            id="ESC-001",
            check_type="cli_stdout_contains",
            params={
                "command": "codeprobe validate {tasks_dir}",
                "fixture": "../project-evil",
            },
        )
        with pytest.raises(ValueError, match="traversal|escape"):
            compile_actions(
                [c],
                target_repo=project,
                workspace=tmp_path / "ws",
                project_root=project,
            )
