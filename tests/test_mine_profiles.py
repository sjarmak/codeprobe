"""Tests for user-defined mine profiles (load, save, list, precedence)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli.mine_cmd import (
    _load_profiles_from,
    list_profiles,
    load_all_profiles,
    load_profile,
    save_profile,
)

# ---------------------------------------------------------------------------
# Unit tests for profile load/save helpers
# ---------------------------------------------------------------------------


class TestLoadProfilesFrom:
    """Verify _load_profiles_from handles edge cases."""

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert _load_profiles_from(tmp_path / "nope.json") == {}

    def test_invalid_json_returns_empty(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        assert _load_profiles_from(bad) == {}

    def test_non_dict_root_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "arr.json"
        f.write_text("[1, 2, 3]", encoding="utf-8")
        assert _load_profiles_from(f) == {}

    def test_filters_non_dict_values(self, tmp_path: Path) -> None:
        f = tmp_path / "mixed.json"
        f.write_text(
            json.dumps({"good": {"count": 10}, "bad": "string"}),
            encoding="utf-8",
        )
        result = _load_profiles_from(f)
        assert "good" in result
        assert "bad" not in result

    def test_loads_valid_profiles(self, tmp_path: Path) -> None:
        f = tmp_path / "profiles.json"
        data = {"fast": {"count": 3}, "slow": {"count": 20, "enrich": True}}
        f.write_text(json.dumps(data), encoding="utf-8")
        result = _load_profiles_from(f)
        assert result == data


class TestSaveProfile:
    """Verify save_profile writes to user-level config."""

    def test_save_creates_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "codeprobe.cli.mine_cmd._user_profiles_path",
            lambda: tmp_path / ".codeprobe" / "mine-profiles.json",
        )
        path = save_profile("my-setup", {"count": 10, "org_scale": True})
        assert path.is_file()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["my-setup"]["count"] == 10
        assert data["my-setup"]["org_scale"] is True

    def test_save_preserves_existing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profiles_path = tmp_path / ".codeprobe" / "mine-profiles.json"
        profiles_path.parent.mkdir(parents=True)
        profiles_path.write_text(
            json.dumps({"existing": {"count": 5}}), encoding="utf-8"
        )
        monkeypatch.setattr(
            "codeprobe.cli.mine_cmd._user_profiles_path",
            lambda: profiles_path,
        )
        save_profile("new-one", {"enrich": True})
        data = json.loads(profiles_path.read_text(encoding="utf-8"))
        assert "existing" in data
        assert "new-one" in data

    def test_save_filters_unknown_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "codeprobe.cli.mine_cmd._user_profiles_path",
            lambda: tmp_path / ".codeprobe" / "mine-profiles.json",
        )
        save_profile("filtered", {"count": 3, "unknown_key": "ignored"})
        data = json.loads(
            (tmp_path / ".codeprobe" / "mine-profiles.json").read_text(encoding="utf-8")
        )
        assert "unknown_key" not in data["filtered"]
        assert data["filtered"]["count"] == 3


class TestLoadProfile:
    """Verify load_profile with precedence and error handling."""

    def test_load_missing_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "codeprobe.cli.mine_cmd._user_profiles_path",
            lambda: tmp_path / "user.json",
        )
        monkeypatch.setattr(
            "codeprobe.cli.mine_cmd._project_profiles_path",
            lambda repo_path=None: tmp_path / "project.json",
        )
        import click

        with pytest.raises(click.UsageError, match="not found"):
            load_profile("nope")

    def test_project_overrides_user(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_file = tmp_path / "user.json"
        user_file.write_text(
            json.dumps({"shared": {"count": 5, "source": "github"}}),
            encoding="utf-8",
        )
        project_file = tmp_path / "project.json"
        project_file.write_text(
            json.dumps({"shared": {"count": 15}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "codeprobe.cli.mine_cmd._user_profiles_path",
            lambda: user_file,
        )
        monkeypatch.setattr(
            "codeprobe.cli.mine_cmd._project_profiles_path",
            lambda repo_path=None: project_file,
        )
        prof = load_profile("shared")
        # Project-level wins: count=15, no source key
        assert prof["count"] == 15
        assert "source" not in prof


class TestLoadAllProfiles:
    """Verify load_all_profiles merges user + project."""

    def test_merges_both_levels(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_file = tmp_path / "user.json"
        user_file.write_text(
            json.dumps({"user-only": {"count": 3}, "both": {"count": 5}}),
            encoding="utf-8",
        )
        project_file = tmp_path / "project.json"
        project_file.write_text(
            json.dumps({"project-only": {"enrich": True}, "both": {"count": 10}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "codeprobe.cli.mine_cmd._user_profiles_path",
            lambda: user_file,
        )
        monkeypatch.setattr(
            "codeprobe.cli.mine_cmd._project_profiles_path",
            lambda repo_path=None: project_file,
        )
        result = load_all_profiles()
        assert result["user-only"][1] == "user"
        assert result["project-only"][1] == "project"
        assert result["both"][1] == "project"
        assert result["both"][0]["count"] == 10


class TestListProfiles:
    """Verify list_profiles returns sorted tuples."""

    def test_returns_sorted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_file = tmp_path / "user.json"
        user_file.write_text(
            json.dumps({"beta": {"count": 2}, "alpha": {"count": 1}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "codeprobe.cli.mine_cmd._user_profiles_path",
            lambda: user_file,
        )
        monkeypatch.setattr(
            "codeprobe.cli.mine_cmd._project_profiles_path",
            lambda repo_path=None: tmp_path / "nope.json",
        )
        entries = list_profiles()
        names = [e[0] for e in entries]
        assert names == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class TestCLIListProfiles:
    """Test --list-profiles via CliRunner."""

    def test_list_profiles_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "codeprobe.cli.mine_cmd._user_profiles_path",
            lambda: tmp_path / "user.json",
        )
        monkeypatch.setattr(
            "codeprobe.cli.mine_cmd._project_profiles_path",
            lambda repo_path=None: tmp_path / "project.json",
        )
        from codeprobe.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["mine", "--list-profiles", str(tmp_path)])
        assert result.exit_code == 0
        assert "No profiles found" in result.output

    def test_list_profiles_shows_entries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_file = tmp_path / "user.json"
        user_file.write_text(json.dumps({"fast": {"count": 3}}), encoding="utf-8")
        monkeypatch.setattr(
            "codeprobe.cli.mine_cmd._user_profiles_path",
            lambda: user_file,
        )
        monkeypatch.setattr(
            "codeprobe.cli.mine_cmd._project_profiles_path",
            lambda repo_path=None: tmp_path / "project.json",
        )
        from codeprobe.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["mine", "--list-profiles", str(tmp_path)])
        assert result.exit_code == 0
        assert "fast" in result.output
        assert "user" in result.output


class TestCLISaveProfile:
    """Test --save-profile via CliRunner."""

    def test_save_profile_creates_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        profiles_path = tmp_path / ".codeprobe" / "mine-profiles.json"
        monkeypatch.setattr(
            "codeprobe.cli.mine_cmd._user_profiles_path",
            lambda: profiles_path,
        )
        from codeprobe.cli import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "mine",
                "--save-profile",
                "my-setup",
                "--count",
                "10",
                "--org-scale",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0
        assert "saved" in result.output
        data = json.loads(profiles_path.read_text(encoding="utf-8"))
        assert data["my-setup"]["count"] == 10
        assert data["my-setup"]["org_scale"] is True


class TestCLIProfileLoad:
    """Test --profile loads values and CLI flags override them."""

    def test_profile_values_passed_to_run_mine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_file = tmp_path / "user.json"
        user_file.write_text(
            json.dumps({"my-setup": {"count": 10, "org_scale": True}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "codeprobe.cli.mine_cmd._user_profiles_path",
            lambda: user_file,
        )
        monkeypatch.setattr(
            "codeprobe.cli.mine_cmd._project_profiles_path",
            lambda repo_path=None: tmp_path / "project.json",
        )

        captured: dict = {}

        def fake_run_mine(path: str, **kwargs: object) -> None:
            captured.update(kwargs)

        monkeypatch.setattr("codeprobe.cli.mine_cmd.run_mine", fake_run_mine)

        from codeprobe.cli import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["mine", "--profile", "my-setup", str(tmp_path)],
        )
        assert result.exit_code == 0
        assert captured["count"] == 10
        assert captured["org_scale"] is True

    def test_explicit_flag_overrides_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_file = tmp_path / "user.json"
        user_file.write_text(
            json.dumps({"my-setup": {"count": 10, "org_scale": True}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "codeprobe.cli.mine_cmd._user_profiles_path",
            lambda: user_file,
        )
        monkeypatch.setattr(
            "codeprobe.cli.mine_cmd._project_profiles_path",
            lambda repo_path=None: tmp_path / "project.json",
        )

        captured: dict = {}

        def fake_run_mine(path: str, **kwargs: object) -> None:
            captured.update(kwargs)

        monkeypatch.setattr("codeprobe.cli.mine_cmd.run_mine", fake_run_mine)

        from codeprobe.cli import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["mine", "--profile", "my-setup", "--count", "5", str(tmp_path)],
        )
        assert result.exit_code == 0
        # Explicit --count 5 overrides profile count=10
        assert captured["count"] == 5
        # Profile value for org_scale still applies
        assert captured["org_scale"] is True

    def test_missing_profile_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "codeprobe.cli.mine_cmd._user_profiles_path",
            lambda: tmp_path / "user.json",
        )
        monkeypatch.setattr(
            "codeprobe.cli.mine_cmd._project_profiles_path",
            lambda repo_path=None: tmp_path / "project.json",
        )

        from codeprobe.cli import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["mine", "--profile", "nonexistent", str(tmp_path)],
        )
        assert result.exit_code != 0
        assert "not found" in result.output
