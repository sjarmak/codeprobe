"""Regression coverage for the install-provenance guard (codeprobe-v3wn).

Reproduces the shared-``.venv`` stale-worktree binding as fabricated venv +
source layouts under ``tmp_path`` and asserts the guard detects it, passes the
correct layout, and stays silent for non-checkout installs.
"""

from __future__ import annotations

import logging
from pathlib import Path

from codeprobe import provenance


def _make_venv(root: Path, *, with_script: bool = True) -> Path:
    """Create a minimal fake venv at ``root/.venv`` and return its path."""
    venv = root / ".venv"
    bindir = venv / provenance._bin_name()
    bindir.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    (bindir / "python").write_text("", encoding="utf-8")
    if with_script:
        (bindir / "codeprobe").write_text("#!fake\n", encoding="utf-8")
    return venv


def _make_src(root: Path) -> Path:
    """Create ``root/src/codeprobe/__init__.py`` and return the __init__."""
    init = root / "src" / "codeprobe" / "__init__.py"
    init.parent.mkdir(parents=True)
    init.write_text("__version__ = '0.0.0'\n", encoding="utf-8")
    return init


class TestAnalyze:
    def test_cross_venv_script_detected(self, tmp_path: Path) -> None:
        """Console script in venv A launched via venv B's interpreter."""
        proj_a = tmp_path / "projA"
        proj_b = tmp_path / "worktrees" / "wt7"
        venv_a = _make_venv(proj_a)
        venv_b = _make_venv(proj_b, with_script=False)
        _make_src(proj_b)

        report = provenance.analyze(
            package_file=proj_b / "src" / "codeprobe" / "__init__.py",
            prefix=venv_b,
            argv0=venv_a / provenance._bin_name() / "codeprobe",
        )

        assert report.ok is False
        assert report.kind == "cross_venv_script"
        assert str(venv_a) in report.detail
        # Repair points the script's OWN venv at its OWN checkout, safely.
        assert "pip install -e" in report.fix
        assert "--force-reinstall" in report.fix
        assert "--no-deps" in report.fix
        assert str(proj_a) in report.fix
        assert "rm" not in report.fix  # never deletes a worktree

    def test_foreign_module_detected(self, tmp_path: Path) -> None:
        """Venv sits next to a checkout but imports from a foreign tree."""
        project = tmp_path / "codeprobe"
        stale = tmp_path / "codeprobe" / "worktrees" / "isun"
        venv = _make_venv(project, with_script=False)
        _make_src(project)
        foreign_init = _make_src(stale)

        report = provenance.analyze(
            package_file=foreign_init,
            prefix=venv,
            # argv0 is not a console script, so Signal A is skipped.
            argv0="python",
        )

        assert report.ok is False
        assert report.kind == "foreign_module"
        assert str(stale.resolve()) in report.detail
        assert str(project.resolve()) in report.fix
        assert "--force-reinstall" in report.fix

    def test_correct_layout_passes(self, tmp_path: Path) -> None:
        """Script, interpreter, and module all agree on one checkout."""
        project = tmp_path / "codeprobe"
        venv = _make_venv(project)
        init = _make_src(project)

        report = provenance.analyze(
            package_file=init,
            prefix=venv,
            argv0=venv / provenance._bin_name() / "codeprobe",
        )

        assert report.ok is True
        assert report.kind == "ok"
        assert report.fix == ""

    def test_correct_worktree_dev_passes(self, tmp_path: Path) -> None:
        """A worktree running its OWN venv is legitimate, not foreign."""
        worktree = tmp_path / "codeprobe-worktrees" / "wt-abc"
        venv = _make_venv(worktree)
        init = _make_src(worktree)

        report = provenance.analyze(
            package_file=init,
            prefix=venv,
            argv0=venv / provenance._bin_name() / "codeprobe",
        )

        assert report.ok is True
        assert report.kind == "ok"

    def test_non_checkout_install_not_applicable(self, tmp_path: Path) -> None:
        """A global/non-venv install has nothing to compare against."""
        prefix = tmp_path / "usr"  # no pyvenv.cfg, no adjacent src
        prefix.mkdir()
        site = tmp_path / "site-packages" / "codeprobe" / "__init__.py"
        site.parent.mkdir(parents=True)
        site.write_text("", encoding="utf-8")

        report = provenance.analyze(
            package_file=site,
            prefix=prefix,
            argv0="codeprobe",  # bare name, not a resolvable script file
        )

        assert report.ok is True
        assert report.kind == "not_applicable"

    def test_totally_bad_inputs_do_not_raise(self) -> None:
        """analyze must be total on the CLI hot path."""
        report = provenance.analyze(package_file=None, prefix="", argv0=None)
        assert report.ok is True


class TestHelpers:
    def test_script_venv_requires_bin_and_pyvenv(self, tmp_path: Path) -> None:
        venv = _make_venv(tmp_path / "p")
        script = venv / provenance._bin_name() / "codeprobe"
        assert provenance._script_venv(script) == venv.resolve()

    def test_script_venv_rejects_bare_name(self) -> None:
        assert provenance._script_venv("codeprobe") is None

    def test_script_venv_rejects_non_venv_dir(self, tmp_path: Path) -> None:
        bindir = tmp_path / "usr" / "bin"
        bindir.mkdir(parents=True)
        script = bindir / "codeprobe"
        script.write_text("", encoding="utf-8")
        assert provenance._script_venv(script) is None  # no pyvenv.cfg

    def test_repair_command_uses_venv_python(self, tmp_path: Path) -> None:
        venv = tmp_path / "proj" / ".venv"
        root = tmp_path / "proj"
        cmd = provenance.repair_command(venv, root)
        assert str(venv) in cmd
        assert str(root) in cmd
        assert cmd.endswith("--force-reinstall --no-deps")


class TestDoctorCheck:
    def test_failing_report_becomes_failing_check(self, monkeypatch: object) -> None:
        from codeprobe.cli import doctor_cmd

        bad = provenance.ProvenanceReport(
            ok=False,
            kind="cross_venv_script",
            detail="foreign binding",
            fix="run the repair",
        )
        monkeypatch.setattr("codeprobe.provenance.analyze", lambda **_: bad)
        result = doctor_cmd._check_install_provenance()
        assert result.name == "install provenance"
        assert result.passed is False
        assert result.detail == "foreign binding"
        assert result.fix == "run the repair"

    def test_included_in_run_checks(self, monkeypatch: object) -> None:
        from codeprobe.cli.doctor_cmd import run_checks

        # Avoid slow/networked checks influencing the list; we only assert the
        # provenance check is present.
        names = {r.name for r in run_checks()}
        assert "install provenance" in names


class TestStartupGuard:
    def test_warns_once_on_foreign(self, monkeypatch, caplog) -> None:
        from codeprobe import cli as cli_pkg

        cli_pkg._provenance_warned = False
        monkeypatch.delenv(provenance.SKIP_ENV, raising=False)
        bad = provenance.ProvenanceReport(ok=False, kind="foreign_module", detail="stale bind", fix="repair now")
        monkeypatch.setattr("codeprobe.provenance.analyze", lambda **_: bad)

        with caplog.at_level(logging.WARNING, logger="codeprobe"):
            cli_pkg._warn_on_foreign_provenance()
            cli_pkg._warn_on_foreign_provenance()  # second call is a no-op

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "stale bind" in warnings[0].getMessage()
        assert "repair now" in warnings[0].getMessage()

    def test_silent_when_ok(self, monkeypatch, caplog) -> None:
        from codeprobe import cli as cli_pkg

        cli_pkg._provenance_warned = False
        monkeypatch.delenv(provenance.SKIP_ENV, raising=False)
        good = provenance.ProvenanceReport(ok=True, kind="ok", detail="fine", fix="")
        monkeypatch.setattr("codeprobe.provenance.analyze", lambda **_: good)

        with caplog.at_level(logging.WARNING, logger="codeprobe"):
            cli_pkg._warn_on_foreign_provenance()

        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_skip_env_suppresses(self, monkeypatch, caplog) -> None:
        from codeprobe import cli as cli_pkg

        cli_pkg._provenance_warned = False
        monkeypatch.setenv(provenance.SKIP_ENV, "1")

        def _boom(**_: object) -> object:  # must not even be called
            raise AssertionError("analyze called despite skip env")

        monkeypatch.setattr("codeprobe.provenance.analyze", _boom)
        with caplog.at_level(logging.WARNING, logger="codeprobe"):
            cli_pkg._warn_on_foreign_provenance()
        assert not caplog.records
