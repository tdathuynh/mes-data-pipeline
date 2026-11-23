"""Static master data for the simulated plant: machines, shifts and stop reasons.

Everything here is invented for demonstration purposes. It does not describe
any real factory, customer or employer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum


class MachineState(str, Enum):
    """Machine states as reported by the (simulated) PLC / SCADA layer."""

    RUNNING = "RUNNING"
    DOWN = "DOWN"  # unplanned stop, always carries a reason code
    PLANNED_STOP = "PLANNED_STOP"  # breaks, planned maintenance


class StopReason(str, Enum):
    """Stop reason codes. The first group is unplanned, the second planned."""

    MECHANICAL_FAILURE = "MECHANICAL_FAILURE"
    TOOL_CHANGE = "TOOL_CHANGE"
    MATERIAL_SHORTAGE = "MATERIAL_SHORTAGE"
    SETUP_CHANGEOVER = "SETUP_CHANGEOVER"
    QUALITY_CHECK = "QUALITY_CHECK"
    NO_OPERATOR = "NO_OPERATOR"
    BREAK = "BREAK"
    PLANNED_MAINTENANCE = "PLANNED_MAINTENANCE"


PLANNED_REASONS = frozenset({StopReason.BREAK, StopReason.PLANNED_MAINTENANCE})
UNPLANNED_REASONS = frozenset(set(StopReason) - PLANNED_REASONS)


@dataclass(frozen=True)
class Machine:
    machine_id: str
    name: str
    line: str
    machine_type: str
    ideal_cycle_time_s: float  # design cycle time for one good part
    # Simulation knobs (how "healthy" the machine is):
    mean_time_between_stops_min: float
    speed_factor: float  # actual speed vs. ideal, 1.0 = runs at nameplate speed
    base_scrap_rate: float
    base_temp_c: float


MACHINES: tuple[Machine, ...] = (
    Machine("CNC-01", "5-axis mill #1", "LINE-A", "CNC", 45.0, 110, 0.93, 0.015, 38.0),
    Machine("CNC-02", "5-axis mill #2", "LINE-A", "CNC", 52.0, 70, 0.88, 0.030, 40.0),
    Machine("CNC-03", "Lathe #1", "LINE-A", "CNC", 38.0, 140, 0.95, 0.010, 36.0),
    Machine("INJ-01", "Injection molder #1", "LINE-B", "INJECTION", 28.0, 90, 0.90, 0.025, 180.0),
    Machine("PKG-01", "Flow wrapper #1", "LINE-C", "PACKAGING", 2.0, 60, 0.85, 0.008, 32.0),
    Machine("PKG-02", "Case packer #1", "LINE-C", "PACKAGING", 6.0, 95, 0.91, 0.005, 30.0),
)

MACHINES_BY_ID: dict[str, Machine] = {m.machine_id: m for m in MACHINES}


@dataclass(frozen=True)
class Shift:
    code: str
    start: time
    hours: int = 8


# Three 8-hour shifts. Shift C crosses midnight and belongs to the date it starts on.
SHIFTS: tuple[Shift, ...] = (
    Shift("A", time(6, 0)),
    Shift("B", time(14, 0)),
    Shift("C", time(22, 0)),
)
FIRST_SHIFT_START = SHIFTS[0].start


def shift_of(ts: datetime) -> tuple[date, str]:
    """Return ``(shift_date, shift_code)`` for a timestamp.

    >>> shift_of(datetime(2026, 1, 5, 2, 30))   # 02:30 belongs to C of the previous day
    (datetime.date(2026, 1, 4), 'C')
    """
    shift_date = (ts - timedelta(hours=FIRST_SHIFT_START.hour)).date()
    for shift in reversed(SHIFTS):
        start = datetime.combine(shift_date, shift.start)
        if ts >= start:
            return shift_date, shift.code
    raise AssertionError("unreachable: every timestamp falls into a shift")  # pragma: no cover


def shift_boundaries(start: datetime, end: datetime) -> list[datetime]:
    """All shift change instants strictly inside ``(start, end)``, in order."""
    out: list[datetime] = []
    day = start.date() - timedelta(days=1)
    while datetime.combine(day, time()) <= end:
        for shift in SHIFTS:
            b = datetime.combine(day, shift.start)
            if start < b < end:
                out.append(b)
        day += timedelta(days=1)
    return out
