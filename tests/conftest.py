from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from mes_pipeline.models import MachineEvent, ProductionCount

DAY = datetime(2026, 1, 5)


def ev(event_id: str, hhmm: str, state: str, reason: str | None = None, machine: str = "CNC-01") -> MachineEvent:
    h, m = map(int, hhmm.split(":"))
    return MachineEvent(
        event_id=event_id,
        machine_id=machine,
        ts=DAY + timedelta(hours=h, minutes=m),
        state=state,  # type: ignore[arg-type]
        reason_code=reason,  # type: ignore[arg-type]
    )


@pytest.fixture
def shift_a_events() -> list[MachineEvent]:
    """One shift A (06:00-14:00) on CNC-01 with a 30 min break and a 30 min tool change.

    run time       = 4h + 1.5h + 1.5h = 25 200 s
    planned stop   = 1 800 s  ->  planned production time = 27 000 s
    """
    return [
        ev("e1", "06:00", "RUNNING"),
        ev("e2", "10:00", "PLANNED_STOP", "BREAK"),
        ev("e3", "10:30", "RUNNING"),
        ev("e4", "12:00", "DOWN", "TOOL_CHANGE"),
        ev("e5", "12:30", "RUNNING"),
    ]


@pytest.fixture
def shift_a_counts() -> list[ProductionCount]:
    """Eight hourly windows: 480 good + 20 scrap = 500 parts in shift A."""
    scrap = [3, 2, 3, 2, 3, 2, 3, 2]
    return [
        ProductionCount(
            machine_id="CNC-01",
            window_start=DAY + timedelta(hours=6 + i),
            window_s=3600,
            good_count=60,
            scrap_count=s,
        )
        for i, s in enumerate(scrap)
    ]
