from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from mes_pipeline.extract import validate_rows
from mes_pipeline.models import MachineEvent, ProductionCount, SensorReading

GOOD_EVENT: dict[str, Any] = {
    "event_id": "CNC-01-000001",
    "machine_id": "CNC-01",
    "ts": "2026-01-05T06:00:00",
    "state": "DOWN",
    "reason_code": "TOOL_CHANGE",
}


def test_valid_event_parses() -> None:
    e = MachineEvent.model_validate(GOOD_EVENT)
    assert e.state.value == "DOWN"
    assert e.ts.hour == 6


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"machine_id": "CNC-99"}, "unknown machine_id"),
        ({"state": "RUNNNING"}, "Input should be"),
        ({"reason_code": None}, "DOWN events need an unplanned reason_code"),
        ({"reason_code": "BREAK"}, "DOWN events need an unplanned reason_code"),
        ({"state": "RUNNING"}, "must not carry a reason_code"),
        ({"state": "PLANNED_STOP"}, "PLANNED_STOP events need a planned reason_code"),
        ({"ts": "not a time"}, "datetime"),
        ({"extra_field": 1}, "Extra inputs are not permitted"),
    ],
)
def test_invalid_events_are_rejected(patch: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        MachineEvent.model_validate({**GOOD_EVENT, **patch})


@pytest.mark.parametrize(
    "patch",
    [{"scrap_count": -1}, {"good_count": -5}, {"window_s": 0}, {"window_start": None}],
)
def test_invalid_counts_are_rejected(patch: dict[str, Any]) -> None:
    base = {
        "machine_id": "PKG-01",
        "window_start": "2026-01-05T06:00:00",
        "window_s": 300,
        "good_count": 1,
        "scrap_count": 0,
    }
    ProductionCount.model_validate(base)
    with pytest.raises(ValidationError):
        ProductionCount.model_validate({**base, **patch})


def test_sensor_csv_strings_are_coerced_and_fault_codes_rejected() -> None:
    rows = iter(
        [
            (
                2,
                {
                    "machine_id": "CNC-01",
                    "ts": "2026-01-05T06:00:00",
                    "temperature_c": "41.5",
                    "spindle_load_pct": "70",
                },
            ),
            (
                3,
                {
                    "machine_id": "CNC-01",
                    "ts": "2026-01-05T06:01:00",
                    "temperature_c": "999.9",
                    "spindle_load_pct": "70",
                },
            ),
            (
                4,
                {
                    "machine_id": "CNC-01",
                    "ts": "2026-01-05T06:02:00",
                    "temperature_c": "41.6",
                    "spindle_load_pct": None,
                },
            ),
        ]
    )
    result = validate_rows("sensor_readings.csv", rows, SensorReading)
    assert [r.temperature_c for r in result.valid] == [41.5]
    assert [(r.line_no, r.error.split(":")[0]) for r in result.rejects] == [
        (3, "temperature_c"),
        (4, "spindle_load_pct"),
    ]


def test_malformed_json_becomes_a_reject() -> None:
    result = validate_rows("x.jsonl", iter([(7, ValueError("bad json"))]), MachineEvent)
    assert result.valid == []
    assert result.rejects[0].line_no == 7
    assert "malformed JSON" in result.rejects[0].error
