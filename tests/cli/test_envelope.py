"""Tests for ``codeprobe.cli.envelope`` — machine-readable output envelope."""

from __future__ import annotations

import io
import json
from dataclasses import asdict

from codeprobe.cli.envelope import (
    CODEPROBE_VERSION,
    ENVELOPE_SCHEMA_VERSION,
    Envelope,
    ErrorPayload,
    NextStep,
    WarningEntry,
    emit,
)


def test_schema_version_constant() -> None:
    assert ENVELOPE_SCHEMA_VERSION == "1"


def test_codeprobe_version_is_string() -> None:
    assert isinstance(CODEPROBE_VERSION, str)
    assert CODEPROBE_VERSION  # non-empty


def test_record_type_defaults_to_envelope() -> None:
    env = Envelope(command="probe")
    assert env.record_type == "envelope"


def test_asdict_key_order() -> None:
    env = Envelope(command="probe")
    keys = list(asdict(env).keys())
    assert keys == [
        "record_type",
        "ok",
        "command",
        "version",
        "schema_version",
        "exit_code",
        "data",
        "error",
        "warnings",
        "next_steps",
    ]


def test_defaults_are_safe_collections() -> None:
    env = Envelope(command="probe")
    assert env.warnings == []
    assert env.next_steps == []
    # Distinct instances must not share list defaults.
    other = Envelope(command="run")
    assert env.warnings is not other.warnings
    assert env.next_steps is not other.next_steps


def test_asdict_round_trip_recovers_all_fields() -> None:
    env = Envelope(
        command="run",
        exit_code=0,
        data={"task_id": "abc", "score": 1.0},
        warnings=[WarningEntry(code="W001", message="cache miss", detail={"n": 1})],
        next_steps=[NextStep(summary="inspect", command="codeprobe inspect abc")],
    )
    recovered = json.loads(json.dumps(asdict(env), default=str))
    assert recovered["record_type"] == "envelope"
    assert recovered["ok"] is True
    assert recovered["command"] == "run"
    assert recovered["exit_code"] == 0
    assert recovered["data"] == {"task_id": "abc", "score": 1.0}
    assert recovered["error"] is None
    assert recovered["warnings"] == [
        {"code": "W001", "message": "cache miss", "detail": {"n": 1}}
    ]
    assert recovered["next_steps"] == [
        {"summary": "inspect", "command": "codeprobe inspect abc"}
    ]
    assert recovered["schema_version"] == ENVELOPE_SCHEMA_VERSION
    assert recovered["version"] == CODEPROBE_VERSION


def test_emit_writes_single_json_line_and_flushes() -> None:
    class _FlushTrackingStream(io.StringIO):
        def __init__(self) -> None:
            super().__init__()
            self.flush_calls = 0

        def flush(self) -> None:  # type: ignore[override]
            self.flush_calls += 1
            super().flush()

    stream = _FlushTrackingStream()
    env = Envelope(command="probe", data={"hello": "world"})
    emit(env, stream=stream)

    raw = stream.getvalue()
    assert raw.endswith("\n")
    # One line only — no embedded newlines in the JSON body.
    assert raw.count("\n") == 1

    payload = json.loads(raw)
    assert payload["record_type"] == "envelope"
    assert payload["command"] == "probe"
    assert payload["data"] == {"hello": "world"}
    assert stream.flush_calls >= 1


def test_failed_envelope_with_error_payload_serializes() -> None:
    err = ErrorPayload(
        code="E_BAD_INPUT",
        message="missing --task-dir",
        kind="prescriptive",
        terminal=False,
        next_try_flag="--task-dir",
        next_try_value="./tasks",
        detail={"got": None},
    )
    env = Envelope(
        ok=False,
        command="run",
        exit_code=2,
        error=err,
    )

    stream = io.StringIO()
    emit(env, stream=stream)
    payload = json.loads(stream.getvalue())

    assert payload["ok"] is False
    assert payload["exit_code"] == 2
    assert payload["error"]["code"] == "E_BAD_INPUT"
    assert payload["error"]["kind"] == "prescriptive"
    assert payload["error"]["terminal"] is False
    assert payload["error"]["next_try_flag"] == "--task-dir"
    assert payload["error"]["next_try_value"] == "./tasks"
    assert payload["error"]["diagnose_cmd"] is None
    assert payload["error"]["message_for_agent"] is None
    assert payload["error"]["detail"] == {"got": None}


def test_emit_handles_path_via_default_str(tmp_path: object) -> None:
    from pathlib import Path

    p = Path("/tmp/example")
    env = Envelope(command="probe", data={"path": p})

    stream = io.StringIO()
    emit(env, stream=stream)

    payload = json.loads(stream.getvalue())
    assert payload["data"]["path"] == str(p)
