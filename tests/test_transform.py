from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from mes_pipeline.config import MachineState, shift_boundaries, shift_of
from mes_pipeline.models import MachineEvent
from mes_pipeline.transform import StateInterval, assign_shift, dedupe_events, events_to_intervals, split_at_shifts

from .conftest import DAY, ev


@pytest.mark.parametrize(
    ("ts", "expected"),
    [
        (datetime(2026, 1, 5, 6, 0), (date(2026, 1, 5), "A")),
        (datetime(2026, 1, 5, 13, 59, 59), (date(2026, 1, 5), "A")),
        (datetime(2026, 1, 5, 14, 0), (date(2026, 1, 5), "B")),
        (datetime(2026, 1, 5, 22, 0), (date(2026, 1, 5), "C")),
        (datetime(2026, 1, 6, 5, 59), (date(2026, 1, 5), "C")),  # night shift belongs to its start date
        (datetime(2026, 1, 1, 0, 30), (date(2025, 12, 31), "C")),  # across a year boundary
    ],
)
def test_shift_of(ts: datetime, expected: tuple[date, str]) -> None:
    assert shift_of(ts) == expected


def test_vectorised_shift_assignment_agrees_with_scalar() -> None:
    ts = pd.Series(pd.date_range("2026-01-04 00:00", periods=3 * 24 * 4, freq="15min"))
    vec = assign_shift(ts)
    scalar = [shift_of(t.to_pydatetime()) for t in ts]
    assert list(zip(vec["shift_date"], vec["shift"], strict=True)) == scalar


def test_shift_boundaries_are_exclusive() -> None:
    assert shift_boundaries(DAY.replace(hour=6), DAY.replace(hour=14)) == []
    assert shift_boundaries(DAY.replace(hour=12), DAY + timedelta(days=1, hours=7)) == [
        DAY.replace(hour=14),
        DAY.replace(hour=22),
        DAY + timedelta(days=1, hours=6),
    ]


def test_dedupe_and_sort() -> None:
    e1, e2 = ev("a", "06:00", "RUNNING"), ev("b", "07:00", "DOWN", "TOOL_CHANGE")
    other = ev("c", "05:00", "RUNNING", machine="PKG-01")
    unique, removed = dedupe_events([e2, e1, other, e1])
    assert removed == 1
    assert [e.event_id for e in unique] == ["a", "b", "c"]  # sorted by machine, then time


def test_intervals_close_at_next_event_of_same_machine_only() -> None:
    events = [
        ev("a", "06:00", "RUNNING"),
        ev("b", "07:00", "DOWN", "TOOL_CHANGE"),
        ev("c", "06:30", "RUNNING", machine="PKG-01"),
    ]
    unique, _ = dedupe_events(events)
    ivs = events_to_intervals(unique, window_end=DAY.replace(hour=8))
    got = [(i.machine_id, i.start_ts.hour, i.start_ts.minute, i.end_ts.hour) for i in ivs]
    assert got == [("CNC-01", 6, 0, 7), ("CNC-01", 7, 0, 8), ("PKG-01", 6, 30, 8)]


def test_events_after_window_end_are_ignored(shift_a_events: list[MachineEvent]) -> None:
    ivs = events_to_intervals(shift_a_events, window_end=DAY.replace(hour=11))
    assert ivs[-1].end_ts == DAY.replace(hour=11)
    assert sum(i.duration_s for i in ivs) == 5 * 3600


def test_split_at_shifts_preserves_total_duration() -> None:
    iv = StateInterval(
        "CNC-01", "x", MachineState.DOWN, "MECHANICAL_FAILURE", DAY.replace(hour=13), DAY.replace(hour=23)
    )
    parts = split_at_shifts(iv)
    assert [(p.start_ts.hour, p.end_ts.hour) for p in parts] == [(13, 14), (14, 22), (22, 23)]
    assert sum(p.duration_s for p in parts) == iv.duration_s
    assert {p.source_event_id for p in parts} == {"x"}
