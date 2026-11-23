"""Turn validated records into analysis-ready fact tables.

Key steps:

* de-duplicate events (feeds are at-least-once) and order them per machine,
* convert state-change *events* into state *intervals* (start, end, duration),
* split intervals at shift boundaries so every second is attributed to exactly
  one shift,
* tag production counts and sensor readings with their shift.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise

import pandas as pd

from .config import FIRST_SHIFT_START, SHIFTS, MachineState, shift_boundaries, shift_of
from .models import MachineEvent, ProductionCount, SensorReading


@dataclass(frozen=True)
class StateInterval:
    machine_id: str
    source_event_id: str
    state: MachineState
    reason_code: str | None
    start_ts: datetime
    end_ts: datetime

    @property
    def duration_s(self) -> float:
        return (self.end_ts - self.start_ts).total_seconds()


def dedupe_events(events: Sequence[MachineEvent]) -> tuple[list[MachineEvent], int]:
    """Drop repeated ``event_id``s (keeps the first) and sort by machine and time.

    Returns the unique events and how many duplicates were removed.
    """
    seen: set[str] = set()
    unique: list[MachineEvent] = []
    for ev in events:
        if ev.event_id in seen:
            continue
        seen.add(ev.event_id)
        unique.append(ev)
    unique.sort(key=lambda e: (e.machine_id, e.ts, e.event_id))
    return unique, len(events) - len(unique)


def events_to_intervals(events: list[MachineEvent], window_end: datetime) -> list[StateInterval]:
    """Each event lasts until the next event of the same machine (or ``window_end``).

    ``events`` must already be de-duplicated and sorted (see :func:`dedupe_events`).
    """
    out: list[StateInterval] = []
    for i, ev in enumerate(events):
        nxt = events[i + 1] if i + 1 < len(events) else None
        end = min(nxt.ts, window_end) if nxt is not None and nxt.machine_id == ev.machine_id else window_end
        if end <= ev.ts:
            continue  # zero-length (same timestamp as the next event) or after the window
        reason = ev.reason_code.value if ev.reason_code is not None else None
        out.append(StateInterval(ev.machine_id, ev.event_id, ev.state, reason, ev.ts, end))
    return out


def split_at_shifts(interval: StateInterval) -> list[StateInterval]:
    """Cut an interval at every shift change it spans."""
    cuts = [interval.start_ts, *shift_boundaries(interval.start_ts, interval.end_ts), interval.end_ts]
    return [
        StateInterval(interval.machine_id, interval.source_event_id, interval.state, interval.reason_code, a, b)
        for a, b in pairwise(cuts)
    ]


def assign_shift(ts: pd.Series) -> pd.DataFrame:
    """Vectorised equivalent of :func:`config.shift_of` for a datetime Series.

    Relies on the shift pattern being three consecutive 8-hour shifts starting at
    ``FIRST_SHIFT_START``; checked here so a config change cannot silently break it.
    """
    assert all(s.hours == 8 for s in SHIFTS) and len(SHIFTS) == 3
    shifted: pd.Series = ts - pd.Timedelta(hours=FIRST_SHIFT_START.hour, minutes=FIRST_SHIFT_START.minute)
    codes = [s.code for s in SHIFTS]
    out = pd.DataFrame(
        {
            "shift_date": shifted.dt.date,
            "shift": (shifted.dt.hour // 8).map(lambda i: codes[i]),
        },
        index=ts.index,
    )
    return out


@dataclass
class Facts:
    state_intervals: pd.DataFrame
    production: pd.DataFrame
    sensors: pd.DataFrame
    duplicates_removed: int


def build_facts(events: list[MachineEvent], counts: list[ProductionCount], sensors: list[SensorReading]) -> Facts:
    """Build the fact tables. The observation window ends with the last count window."""
    if not counts:
        raise ValueError("no valid production counts: cannot determine the observation window")
    window_end = max(c.window_start + timedelta(seconds=c.window_s) for c in counts)

    unique, removed = dedupe_events(events)
    pieces = [p for iv in events_to_intervals(unique, window_end) for p in split_at_shifts(iv)]
    intervals = pd.DataFrame(
        [
            {
                "machine_id": p.machine_id,
                "source_event_id": p.source_event_id,
                "shift_date": shift_of(p.start_ts)[0],
                "shift": shift_of(p.start_ts)[1],
                "state": p.state.value,
                "reason_code": p.reason_code,
                "start_ts": p.start_ts,
                "end_ts": p.end_ts,
                "duration_s": p.duration_s,
            }
            for p in pieces
        ]
    )

    production = pd.DataFrame([c.model_dump() for c in counts])
    production = production.drop_duplicates(["machine_id", "window_start"]).sort_values(["machine_id", "window_start"])
    production = pd.concat([production, assign_shift(production["window_start"])], axis=1)

    sensor_df = pd.DataFrame([s.model_dump() for s in sensors], columns=list(SensorReading.model_fields))
    sensor_df["ts"] = pd.to_datetime(sensor_df["ts"])
    if sensor_df.empty:
        sensor_df = sensor_df.assign(shift_date=pd.Series(dtype="object"), shift=pd.Series(dtype="object"))
    else:
        sensor_df = sensor_df.sort_values(["machine_id", "ts"])
        sensor_df = pd.concat([sensor_df, assign_shift(sensor_df["ts"])], axis=1)

    return Facts(intervals, production.reset_index(drop=True), sensor_df.reset_index(drop=True), removed)
