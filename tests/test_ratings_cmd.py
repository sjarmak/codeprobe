"""Tests for codeprobe ratings CLI subcommands."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from click.testing import CliRunner

from codeprobe.cli import main


class TestRatingsRecord:
    def test_record_basic(self, tmp_path: Path):
        path = tmp_path / "ratings.jsonl"
        runner = CliRunner()
        result = runner.invoke(main, ["ratings", "record", "4", "--path", str(path)])
        assert result.exit_code == 0
        assert "Recorded" in result.output
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["rating"] == 4

    def test_record_with_task_type(self, tmp_path: Path):
        path = tmp_path / "ratings.jsonl"
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["ratings", "record", "5", "--task-type", "bugfix", "--path", str(path)],
        )
        assert result.exit_code == 0
        data = json.loads(path.read_text().strip())
        assert data["task_type"] == "bugfix"

    def test_record_with_duration_and_tools(self, tmp_path: Path):
        path = tmp_path / "ratings.jsonl"
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "ratings",
                "record",
                "3",
                "--duration",
                "120.5",
                "--tool-calls",
                "15",
                "--path",
                str(path),
            ],
        )
        assert result.exit_code == 0
        data = json.loads(path.read_text().strip())
        assert data["duration_s"] == 120.5
        assert data["tool_calls"] == 15

    def test_record_invalid_rating(self, tmp_path: Path):
        path = tmp_path / "ratings.jsonl"
        runner = CliRunner()
        result = runner.invoke(main, ["ratings", "record", "0", "--path", str(path)])
        assert result.exit_code != 0

    def test_record_rating_out_of_range_high(self, tmp_path: Path):
        path = tmp_path / "ratings.jsonl"
        runner = CliRunner()
        result = runner.invoke(main, ["ratings", "record", "6", "--path", str(path)])
        assert result.exit_code != 0


class TestRatingsSummary:
    def _seed_ratings(self, path: Path, ratings: list[dict]) -> None:
        with open(path, "w") as f:
            for r in ratings:
                f.write(json.dumps(r) + "\n")

    def test_summary_no_file(self, tmp_path: Path):
        path = tmp_path / "ratings.jsonl"
        runner = CliRunner()
        result = runner.invoke(main, ["ratings", "summary", "--path", str(path)])
        assert result.exit_code == 1
        assert "No ratings" in result.output

    def test_summary_with_data(self, tmp_path: Path):
        path = tmp_path / "ratings.jsonl"
        self._seed_ratings(
            path,
            [
                {"ts": "t1", "rating": 5, "config": {"model": "opus"}},
                {"ts": "t2", "rating": 3, "config": {"model": "opus"}},
            ],
        )
        runner = CliRunner()
        result = runner.invoke(main, ["ratings", "summary", "--path", str(path)])
        assert result.exit_code == 0
        assert "2 sessions" in result.output
        assert "opus" in result.output

    def test_summary_low_sample_warning(self, tmp_path: Path):
        path = tmp_path / "ratings.jsonl"
        self._seed_ratings(
            path,
            [
                {"ts": "t1", "rating": 4, "config": {"model": "opus"}},
            ],
        )
        runner = CliRunner()
        result = runner.invoke(main, ["ratings", "summary", "--path", str(path)])
        assert result.exit_code == 0
        assert "Warning" in result.output


class TestRatingsExport:
    def _seed_ratings(self, path: Path, ratings: list[dict]) -> None:
        with open(path, "w") as f:
            for r in ratings:
                f.write(json.dumps(r) + "\n")

    def test_export_csv(self, tmp_path: Path):
        ratings_path = tmp_path / "ratings.jsonl"
        csv_path = tmp_path / "out.csv"
        self._seed_ratings(
            ratings_path,
            [
                {
                    "ts": "t1",
                    "rating": 5,
                    "config": {"model": "opus", "mcps": ["exa"], "skills": []},
                    "task_type": "feature",
                    "duration_s": 100.0,
                    "tool_calls": 10,
                },
            ],
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "ratings",
                "export",
                "--path",
                str(ratings_path),
                "--output",
                str(csv_path),
            ],
        )
        assert result.exit_code == 0
        assert "Exported" in result.output
        assert csv_path.is_file()

        with open(csv_path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["rating"] == "5"
        assert rows[0]["model"] == "opus"

    def test_export_no_file(self, tmp_path: Path):
        ratings_path = tmp_path / "ratings.jsonl"
        csv_path = tmp_path / "out.csv"
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "ratings",
                "export",
                "--path",
                str(ratings_path),
                "--output",
                str(csv_path),
            ],
        )
        assert result.exit_code == 1
        assert "No ratings" in result.output


class TestRatingsRegistered:
    def test_ratings_in_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert "ratings" in result.output

    def test_ratings_subcommands_in_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["ratings", "--help"])
        assert result.exit_code == 0
        for cmd in ("record", "summary", "export"):
            assert cmd in result.output, f"Subcommand '{cmd}' not in ratings help"
