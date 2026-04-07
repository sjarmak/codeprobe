"""Tests for probe generator, writer, and CLI command."""

from __future__ import annotations

import json
import logging
import re
import stat
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli import main

# ---------------------------------------------------------------------------
# Fixture: minimal Python repo for symbol extraction
# ---------------------------------------------------------------------------


@pytest.fixture()
def py_repo(tmp_path: Path) -> Path:
    """Create a tiny Python repo with extractable symbols."""
    pkg = tmp_path / "mylib"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")

    (pkg / "core.py").write_text(
        '''\
class Engine:
    """The main engine class."""

    def start(self) -> bool:
        return True

    def stop(self) -> None:
        pass


def compute_total(items: list[int]) -> int:
    """Sum up items."""
    return sum(items)


def _private_helper() -> None:
    """Should be skipped (private)."""
    pass
''',
        encoding="utf-8",
    )

    (pkg / "utils.py").write_text(
        '''\
from mylib.core import compute_total


def format_output(value: int) -> str:
    """Format a value for display."""
    return f"Total: {value}"
''',
        encoding="utf-8",
    )

    return tmp_path


@pytest.fixture()
def ts_repo(tmp_path: Path) -> Path:
    """Create a tiny TypeScript repo with extractable symbols."""
    src = tmp_path / "src"
    src.mkdir()

    (src / "index.ts").write_text(
        """\
export function greet(name: string): string {
    return `Hello, ${name}!`;
}

export class UserService {
    public findById(id: string): User | null {
        return null;
    }
}
""",
        encoding="utf-8",
    )

    return tmp_path


# ---------------------------------------------------------------------------
# Symbol extraction
# ---------------------------------------------------------------------------


class TestExtractPythonSymbols:
    def test_extracts_functions(self, py_repo: Path) -> None:
        from codeprobe.probe.generator import extract_python_symbols

        content = (py_repo / "mylib" / "core.py").read_text()
        symbols = extract_python_symbols(content, "mylib/core.py")
        names = [s.name for s in symbols]
        assert "compute_total" in names

    def test_extracts_classes(self, py_repo: Path) -> None:
        from codeprobe.probe.generator import extract_python_symbols

        content = (py_repo / "mylib" / "core.py").read_text()
        symbols = extract_python_symbols(content, "mylib/core.py")
        classes = [s for s in symbols if s.kind == "class"]
        assert len(classes) == 1
        assert classes[0].name == "Engine"

    def test_extracts_methods(self, py_repo: Path) -> None:
        from codeprobe.probe.generator import extract_python_symbols

        content = (py_repo / "mylib" / "core.py").read_text()
        symbols = extract_python_symbols(content, "mylib/core.py")
        methods = [s for s in symbols if s.kind == "method"]
        assert any(m.name == "start" for m in methods)
        assert any(m.class_name == "Engine" for m in methods)

    def test_skips_private_functions(self, py_repo: Path) -> None:
        from codeprobe.probe.generator import extract_python_symbols

        content = (py_repo / "mylib" / "core.py").read_text()
        symbols = extract_python_symbols(content, "mylib/core.py")
        names = [s.name for s in symbols]
        assert "_private_helper" not in names

    def test_captures_return_type(self, py_repo: Path) -> None:
        from codeprobe.probe.generator import extract_python_symbols

        content = (py_repo / "mylib" / "core.py").read_text()
        symbols = extract_python_symbols(content, "mylib/core.py")
        func = next(s for s in symbols if s.name == "compute_total")
        assert func.return_type == "int"


class TestExtractTypeScriptSymbols:
    def test_extracts_functions(self, ts_repo: Path) -> None:
        from codeprobe.probe.generator import extract_typescript_symbols

        content = (ts_repo / "src" / "index.ts").read_text()
        symbols = extract_typescript_symbols(content, "src/index.ts")
        names = [s.name for s in symbols]
        assert "greet" in names

    def test_extracts_classes(self, ts_repo: Path) -> None:
        from codeprobe.probe.generator import extract_typescript_symbols

        content = (ts_repo / "src" / "index.ts").read_text()
        symbols = extract_typescript_symbols(content, "src/index.ts")
        classes = [s for s in symbols if s.kind == "class"]
        assert any(c.name == "UserService" for c in classes)

    def test_extracts_methods(self, ts_repo: Path) -> None:
        from codeprobe.probe.generator import extract_typescript_symbols

        content = (ts_repo / "src" / "index.ts").read_text()
        symbols = extract_typescript_symbols(content, "src/index.ts")
        methods = [s for s in symbols if s.kind == "method"]
        assert any(m.name == "findById" for m in methods)


# ---------------------------------------------------------------------------
# collect_symbols
# ---------------------------------------------------------------------------


class TestCollectSymbols:
    def test_collects_from_python_repo(self, py_repo: Path) -> None:
        from codeprobe.probe.generator import collect_symbols

        symbols = collect_symbols(py_repo, lang_filter="python")
        assert len(symbols) > 0
        assert all(s.file_path.endswith(".py") for s in symbols)

    def test_lang_filter_python(self, py_repo: Path) -> None:
        from codeprobe.probe.generator import collect_symbols

        symbols = collect_symbols(py_repo, lang_filter="typescript")
        assert len(symbols) == 0

    def test_skips_pycache(self, py_repo: Path) -> None:
        from codeprobe.probe.generator import collect_symbols

        # Create a __pycache__ dir with a .py file
        cache_dir = py_repo / "__pycache__"
        cache_dir.mkdir()
        (cache_dir / "cached.py").write_text("def cached(): pass", encoding="utf-8")

        symbols = collect_symbols(py_repo, lang_filter="python")
        assert not any("__pycache__" in s.file_path for s in symbols)

    def test_skips_init_files(self, py_repo: Path) -> None:
        from codeprobe.probe.generator import collect_symbols

        symbols = collect_symbols(py_repo, lang_filter="python")
        assert not any("__init__.py" in s.file_path for s in symbols)


# ---------------------------------------------------------------------------
# Symbol dataclass
# ---------------------------------------------------------------------------


class TestSymbol:
    def test_frozen(self) -> None:
        from codeprobe.probe.generator import Symbol

        sym = Symbol(name="foo", kind="function", file_path="a.py", line=1)
        with pytest.raises(AttributeError):
            sym.name = "bar"  # type: ignore[misc]

    def test_defaults(self) -> None:
        from codeprobe.probe.generator import Symbol

        sym = Symbol(name="foo", kind="function", file_path="a.py", line=1)
        assert sym.class_name is None
        assert sym.return_type is None


# ---------------------------------------------------------------------------
# Probe dataclass
# ---------------------------------------------------------------------------


class TestProbe:
    def test_frozen(self) -> None:
        from codeprobe.probe.generator import Probe

        probe = Probe(
            template_name="find_function",
            category="probe_navigate",
            prompt="Where is foo?",
            answer="src/foo.py",
            answer_type="file_path",
            difficulty="easy",
        )
        with pytest.raises(AttributeError):
            probe.prompt = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# generate_probes
# ---------------------------------------------------------------------------


class TestGenerateProbes:
    def test_generates_probes(self, py_repo: Path) -> None:
        from codeprobe.probe.generator import generate_probes

        probes = generate_probes(py_repo, count=5, seed=42)
        assert len(probes) > 0
        assert len(probes) <= 5

    def test_seed_reproducibility(self, py_repo: Path) -> None:
        from codeprobe.probe.generator import generate_probes

        probes_a = generate_probes(py_repo, count=5, seed=42)
        probes_b = generate_probes(py_repo, count=5, seed=42)
        assert probes_a == probes_b

    def test_lang_filter(self, py_repo: Path) -> None:
        from codeprobe.probe.generator import generate_probes

        probes = generate_probes(py_repo, count=5, lang_filter="typescript", seed=42)
        assert len(probes) == 0

    def test_empty_repo(self, tmp_path: Path) -> None:
        from codeprobe.probe.generator import generate_probes

        probes = generate_probes(tmp_path, count=5, seed=42)
        assert probes == []

    # -- Bead 4: INFO-level summary and timing logs --

    def test_generate_probes_logs_symbol_summary(
        self, py_repo: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from codeprobe.probe.generator import generate_probes

        with caplog.at_level(logging.INFO, logger="codeprobe"):
            generate_probes(py_repo, count=5, seed=42)
        summary_records = [
            r
            for r in caplog.records
            if r.name == "codeprobe.probe.generator"
            and "symbol" in r.getMessage().lower()
        ]
        assert len(summary_records) >= 1
        msg = summary_records[0].getMessage()
        assert "function" in msg or "class" in msg or "method" in msg

    def test_generate_probes_logs_per_template_counts(
        self, py_repo: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from codeprobe.probe.generator import generate_probes

        with caplog.at_level(logging.INFO, logger="codeprobe"):
            generate_probes(py_repo, count=5, seed=42)
        template_records = [
            r
            for r in caplog.records
            if r.name == "codeprobe.probe.generator" and "Generated" in r.getMessage()
        ]
        assert len(template_records) >= 1
        msg = template_records[0].getMessage()
        assert re.search(r"\d+", msg)
        assert "find_function" in msg or "count_callers" in msg

    def test_generate_probes_logs_wall_clock_time(
        self, py_repo: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from codeprobe.probe.generator import generate_probes

        with caplog.at_level(logging.INFO, logger="codeprobe"):
            generate_probes(py_repo, count=5, seed=42)
        time_records = [
            r
            for r in caplog.records
            if r.name == "codeprobe.probe.generator"
            and re.search(r"\d+(\.\d+)?\s*s", r.getMessage())
        ]
        assert len(time_records) >= 1

    def test_generate_probes_empty_repo_still_logs(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from codeprobe.probe.generator import generate_probes

        with caplog.at_level(logging.INFO, logger="codeprobe"):
            probes = generate_probes(tmp_path)
        assert probes == []
        summary_records = [
            r
            for r in caplog.records
            if r.name == "codeprobe.probe.generator"
            and "symbol" in r.getMessage().lower()
        ]
        assert len(summary_records) >= 1
        msg = summary_records[0].getMessage()
        assert "0" in msg


# ---------------------------------------------------------------------------
# Probe logging — DEBUG level (bead 5)
# ---------------------------------------------------------------------------


class TestSlowGenerationWarning:
    def test_generate_probes_warns_on_slow_run(
        self,
        py_repo: Path,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from codeprobe.probe.generator import generate_probes

        # Simulate >60s elapsed by patching time.perf_counter
        call_count = 0

        def fake_perf_counter() -> float:
            nonlocal call_count
            call_count += 1
            # First call returns 0.0, all subsequent return 65.0
            return 0.0 if call_count == 1 else 65.0

        import codeprobe.probe.generator as gen_mod

        monkeypatch.setattr(gen_mod.time, "perf_counter", fake_perf_counter)

        with caplog.at_level(logging.WARNING, logger="codeprobe"):
            generate_probes(py_repo, count=5, seed=42)
        warning_records = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING
            and ("--lang" in r.getMessage() or "--count" in r.getMessage())
        ]
        assert len(warning_records) >= 1

    def test_generate_probes_no_warning_on_fast_run(
        self, py_repo: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from codeprobe.probe.generator import generate_probes

        with caplog.at_level(logging.WARNING, logger="codeprobe"):
            generate_probes(py_repo, count=5, seed=42)
        warning_records = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING
            and "Probe generation took" in r.getMessage()
        ]
        assert len(warning_records) == 0

    def test_collect_symbols_debug_logs_per_file(
        self, py_repo: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from codeprobe.probe.generator import collect_symbols

        with caplog.at_level(logging.DEBUG, logger="codeprobe"):
            collect_symbols(py_repo)
        debug_records = [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG
            and r.name == "codeprobe.probe.generator"
            and "extracted" in r.getMessage()
        ]
        assert len(debug_records) >= 1

    def test_collect_symbols_debug_logs_skip_reason(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        # A binary .py file
        (pkg / "fake.py").write_bytes(b"\x00\x01\x02")
        # A normal .py file
        (pkg / "good.py").write_text("def hello(): pass\n", encoding="utf-8")

        from codeprobe.probe.generator import collect_symbols

        with caplog.at_level(logging.DEBUG, logger="codeprobe"):
            collect_symbols(tmp_path)
        skip_records = [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG
            and "skip" in r.getMessage()
            and "fake.py" in r.getMessage()
        ]
        assert len(skip_records) >= 1

    def test_collect_symbols_does_not_log_skip_at_info_level(
        self, py_repo: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from codeprobe.probe.generator import collect_symbols

        with caplog.at_level(logging.INFO, logger="codeprobe"):
            collect_symbols(py_repo)
        skip_records = [r for r in caplog.records if "skip" in r.getMessage().lower()]
        assert len(skip_records) == 0

    def test_compute_caller_count_debug_logs_timing(
        self, py_repo: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from codeprobe.probe.generator import compute_caller_count

        with caplog.at_level(logging.DEBUG, logger="codeprobe"):
            compute_caller_count(py_repo, "compute_total")
        timing_records = [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG
            and "caller count" in r.getMessage()
            and re.search(r"\d+(\.\d+)?\s*s", r.getMessage())
        ]
        assert len(timing_records) >= 1


# ---------------------------------------------------------------------------
# write_probe_tasks
# ---------------------------------------------------------------------------


class TestWriteProbeTasks:
    def test_writes_task_dirs(self, py_repo: Path, tmp_path: Path) -> None:
        from codeprobe.probe.generator import generate_probes
        from codeprobe.probe.writer import write_probe_tasks

        probes = generate_probes(py_repo, count=3, seed=42)
        assert len(probes) > 0
        created = write_probe_tasks(probes, tmp_path / "output", repo_name="test-repo")
        assert len(created) == len(probes)
        for task_dir in created:
            assert (task_dir / "instruction.md").is_file()
            assert (task_dir / "task.toml").is_file()
            assert (task_dir / "tests" / "test.sh").is_file()
            assert (task_dir / "tests" / "ground_truth.json").is_file()

    def test_task_toml_is_valid(self, py_repo: Path, tmp_path: Path) -> None:
        from codeprobe.probe.generator import generate_probes
        from codeprobe.probe.writer import write_probe_tasks

        probes = generate_probes(py_repo, count=1, seed=42)
        created = write_probe_tasks(probes, tmp_path / "output")
        with (created[0] / "task.toml").open("rb") as f:
            data = tomllib.load(f)
        assert "task" in data
        assert "metadata" in data
        assert data["metadata"]["difficulty"] in ("easy", "medium", "hard")

    def test_test_sh_is_executable(self, py_repo: Path, tmp_path: Path) -> None:
        from codeprobe.probe.generator import generate_probes
        from codeprobe.probe.writer import write_probe_tasks

        probes = generate_probes(py_repo, count=1, seed=42)
        created = write_probe_tasks(probes, tmp_path / "output")
        test_sh = created[0] / "tests" / "test.sh"
        mode = test_sh.stat().st_mode
        assert mode & stat.S_IXUSR

    def test_ground_truth_json(self, py_repo: Path, tmp_path: Path) -> None:
        from codeprobe.probe.generator import generate_probes
        from codeprobe.probe.writer import write_probe_tasks

        probes = generate_probes(py_repo, count=1, seed=42)
        created = write_probe_tasks(probes, tmp_path / "output")
        gt = json.loads((created[0] / "tests" / "ground_truth.json").read_text())
        assert "answer" in gt
        assert "answer_type" in gt
        assert "template" in gt

    def test_test_sh_reads_agent_output_env_var(
        self, py_repo: Path, tmp_path: Path
    ) -> None:
        """test.sh must read from $AGENT_OUTPUT first (sandbox contract)."""
        from codeprobe.probe.generator import generate_probes
        from codeprobe.probe.writer import write_probe_tasks

        probes = generate_probes(py_repo, count=1, seed=42)
        created = write_probe_tasks(probes, tmp_path / "output")
        test_sh = (created[0] / "tests" / "test.sh").read_text()
        assert "AGENT_OUTPUT" in test_sh

    def test_test_sh_passes_with_agent_output_env(
        self, py_repo: Path, tmp_path: Path
    ) -> None:
        """test.sh scores correctly when $AGENT_OUTPUT points to answer file."""
        import subprocess

        from codeprobe.probe.generator import generate_probes
        from codeprobe.probe.writer import write_probe_tasks

        probes = generate_probes(py_repo, count=1, seed=42)
        # Pick the first find_function probe (answer is a file path)
        ff_probes = [p for p in probes if p.template_name == "find_function"]
        if not ff_probes:
            pytest.skip("No find_function probe generated")
        created = write_probe_tasks(ff_probes[:1], tmp_path / "output")
        task_dir = created[0]
        gt = json.loads((task_dir / "tests" / "ground_truth.json").read_text())

        # Write correct answer to a file and pass via AGENT_OUTPUT
        answer_file = tmp_path / "agent_output.txt"
        answer_file.write_text(gt["answer"], encoding="utf-8")

        result = subprocess.run(
            ["bash", str(task_dir / "tests" / "test.sh")],
            env={**{"AGENT_OUTPUT": str(answer_file), "PATH": "/usr/bin:/bin"}},
            cwd=str(task_dir),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"stdout={result.stdout} stderr={result.stderr}"
        assert "PASS" in result.stdout

    def test_test_sh_fails_with_wrong_answer(
        self, py_repo: Path, tmp_path: Path
    ) -> None:
        """test.sh exits non-zero when given the wrong answer."""
        import subprocess

        from codeprobe.probe.generator import generate_probes
        from codeprobe.probe.writer import write_probe_tasks

        probes = generate_probes(py_repo, count=1, seed=42)
        ff_probes = [p for p in probes if p.template_name == "find_function"]
        if not ff_probes:
            pytest.skip("No find_function probe generated")
        created = write_probe_tasks(ff_probes[:1], tmp_path / "output")
        task_dir = created[0]

        answer_file = tmp_path / "agent_output.txt"
        answer_file.write_text("totally/wrong/path.py", encoding="utf-8")

        result = subprocess.run(
            ["bash", str(task_dir / "tests" / "test.sh")],
            env={**{"AGENT_OUTPUT": str(answer_file), "PATH": "/usr/bin:/bin"}},
            cwd=str(task_dir),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode != 0
        assert "FAIL" in result.stdout


# ---------------------------------------------------------------------------
# CLI: codeprobe probe
# ---------------------------------------------------------------------------


class TestProbeCLI:
    def test_probe_command_registered(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["probe", "--help"])
        assert result.exit_code == 0
        assert "Generate" in result.output or "generate" in result.output

    def test_probe_generates_tasks(self, py_repo: Path, tmp_path: Path) -> None:
        runner = CliRunner()
        output_dir = tmp_path / "probes"
        result = runner.invoke(
            main,
            [
                "probe",
                str(py_repo),
                "--count",
                "3",
                "--output",
                str(output_dir),
                "--seed",
                "42",
            ],
        )
        assert result.exit_code == 0, result.output
        assert output_dir.exists()
        # Should have created task subdirectories
        task_dirs = [d for d in output_dir.iterdir() if d.is_dir()]
        assert len(task_dirs) > 0

    def test_probe_lang_filter(self, py_repo: Path, tmp_path: Path) -> None:
        runner = CliRunner()
        output_dir = tmp_path / "probes"
        result = runner.invoke(
            main,
            [
                "probe",
                str(py_repo),
                "--count",
                "3",
                "--lang",
                "python",
                "--output",
                str(output_dir),
                "--seed",
                "42",
            ],
        )
        assert result.exit_code == 0, result.output

    def test_probe_no_symbols_exits_cleanly(self, tmp_path: Path) -> None:
        runner = CliRunner()
        empty_repo = tmp_path / "empty"
        empty_repo.mkdir()
        result = runner.invoke(
            main,
            ["probe", str(empty_repo), "--output", str(tmp_path / "out")],
        )
        # Should exit with code 1 and a message about no symbols
        assert result.exit_code == 1

    def test_probe_count_clamped(self, py_repo: Path, tmp_path: Path) -> None:
        runner = CliRunner()
        output_dir = tmp_path / "probes"
        # Count of 100 should be clamped to MAX_PROBES (50)
        result = runner.invoke(
            main,
            [
                "probe",
                str(py_repo),
                "--count",
                "100",
                "--output",
                str(output_dir),
                "--seed",
                "42",
            ],
        )
        assert result.exit_code == 0, result.output

    def test_probe_json_summary(self, py_repo: Path, tmp_path: Path) -> None:
        runner = CliRunner()
        output_dir = tmp_path / "probes"
        result = runner.invoke(
            main,
            [
                "probe",
                str(py_repo),
                "--count",
                "3",
                "--output",
                str(output_dir),
                "--seed",
                "42",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        # Extract the JSON object from output (may contain log lines before it)
        json_start = result.output.index("{")
        data = json.loads(result.output[json_start:])
        assert "total" in data
        assert "by_template" in data

    def test_probe_quiet_suppresses_progress_messages(
        self, py_repo: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        runner = CliRunner()
        output_dir = tmp_path / "probes"
        with caplog.at_level(logging.DEBUG, logger="codeprobe"):
            result = runner.invoke(
                main,
                [
                    "-q",
                    "probe",
                    str(py_repo),
                    "--count",
                    "3",
                    "--output",
                    str(output_dir),
                    "--seed",
                    "42",
                ],
            )
        assert result.exit_code == 0, result.output
        # With -q, the codeprobe logger is set to WARNING, so INFO
        # messages like "Scanning" and "Probe generation complete"
        # should not appear in the captured log records.
        info_messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert not any("Scanning" in m for m in info_messages)
        assert not any("Probe generation complete" in m for m in info_messages)

    def test_probe_default_emits_info_progress(
        self, py_repo: Path, tmp_path: Path
    ) -> None:
        runner = CliRunner()
        output_dir = tmp_path / "probes"
        result = runner.invoke(
            main,
            [
                "probe",
                str(py_repo),
                "--count",
                "3",
                "--output",
                str(output_dir),
                "--seed",
                "42",
            ],
        )
        assert result.exit_code == 0, result.output
        # Default verbosity is INFO; the StreamHandler writes to stderr
        # which Click's CliRunner captures in result.output.
        assert "Scanning" in result.output
        assert "Probe generation complete" in result.output

    def test_probe_no_symbols_logs_warning(self, tmp_path: Path) -> None:
        runner = CliRunner()
        empty_repo = tmp_path / "empty"
        empty_repo.mkdir()
        result = runner.invoke(
            main,
            ["probe", str(empty_repo), "--output", str(tmp_path / "out")],
        )
        assert result.exit_code == 1
        # The warning is emitted via the logging StreamHandler to stderr,
        # captured in result.output by CliRunner.
        assert "no suitable symbols" in result.output

    def test_probe_final_summary_on_stdout(self, py_repo: Path, tmp_path: Path) -> None:
        runner = CliRunner()
        output_dir = tmp_path / "probes"
        result = runner.invoke(
            main,
            [
                "probe",
                str(py_repo),
                "--count",
                "3",
                "--output",
                str(output_dir),
                "--seed",
                "42",
            ],
        )
        assert result.exit_code == 0, result.output
        # The final summary line uses click.echo (stdout), not logger.
        # It should be in the output.
        assert "Created" in result.output
        assert "probe tasks in" in result.output
