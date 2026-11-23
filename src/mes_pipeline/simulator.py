"""Synthetic shop-floor data generator.

Produces the three raw feeds a typical MES ingests from the shop floor:

* ``machine_events.jsonl``    - state-change events (RUNNING / DOWN / PLANNED_STOP + reason)
* ``production_counts.jsonl`` - good / scrap counters per machine, every 5 minutes
* ``sensor_readings.csv``     - temperature and spindle/motor load, every minute

All data is **synthetic** and generated from a seeded random model, so runs are
reproducible. Optionally the generator injects realistic data-quality defects
(duplicates, typos, unknown machines, sensor fault codes, negative counters) so
the validation layer has something to catch.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from .config import MACHINES, SHIFTS, Machine, MachineState, StopReason

COUNT_WINDOW_MIN = 5
DEFAULT_START = datetime(2026, 1, 5, 6, 0)  # a Monday, start of shift A

# Unplanned stop profile: (relative weight, mean duration in minutes)
_STOP_PROFILE: dict[StopReason, tuple[float, float]] = {
    StopReason.MECHANICAL_FAILURE: (1.0, 40.0),
    StopReason.TOOL_CHANGE: (3.0, 9.0),
    StopReason.MATERIAL_SHORTAGE: (1.5, 22.0),
    StopReason.SETUP_CHANGEOVER: (1.2, 28.0),
    StopReason.QUALITY_CHECK: (2.0, 7.0),
    StopReason.NO_OPERATOR: (0.8, 15.0),
}
_TEMP_RISE_RUNNING_C = {"CNC": 12.0, "INJECTION": 25.0, "PACKAGING": 6.0}

# Integer codes used for the minute-level timeline
_RUN, _DOWN, _PLANNED = 0, 1, 2


@dataclass
class SimulatedData:
    events: list[dict[str, Any]] = field(default_factory=list)
    counts: list[dict[str, Any]] = field(default_factory=list)
    sensors: list[dict[str, Any]] = field(default_factory=list)


def _iso(ts: datetime) -> str:
    return ts.isoformat(timespec="seconds")


def _timeline(
    machine: Machine, idx: int, start: datetime, n_min: int, rng: np.random.Generator
) -> tuple[npt.NDArray[np.int8], list[str | None]]:
    """Minute-by-minute state and reason for one machine."""
    state = np.full(n_min, _RUN, dtype=np.int8)
    reason: list[str | None] = [None] * n_min
    reasons = list(_STOP_PROFILE)

    # 1) Alternate RUNNING spells and unplanned stops.
    t = 0
    while t < n_min:
        t += max(1, int(rng.exponential(machine.mean_time_between_stops_min)))
        if t >= n_min:
            break
        ts = start + timedelta(minutes=t)
        weights = np.array([_STOP_PROFILE[r][0] for r in reasons])
        if ts.hour >= 22 or ts.hour < 6:  # night shift: staffing gaps are more common
            weights[reasons.index(StopReason.NO_OPERATOR)] *= 3
        weights = weights / weights.sum()
        r = reasons[rng.choice(len(reasons), p=weights)]
        mean = _STOP_PROFILE[r][1]
        dur = max(2, int(rng.lognormal(np.log(mean), 0.5)))
        end = min(n_min, t + dur)
        state[t:end] = _DOWN
        reason[t:end] = [r.value] * (end - t)
        t = end

    # 2) Planned stops override everything: a 30-min break mid-shift and a weekly
    #    2-hour planned maintenance slot on Sundays (staggered per machine).
    day0 = start.replace(hour=0, minute=0)
    days = n_min // 1440 + 2
    for d in range(-1, days):
        day = day0 + timedelta(days=d)
        windows: list[tuple[datetime, int, StopReason]] = [
            (day.replace(hour=s.start.hour) + timedelta(hours=4), 30, StopReason.BREAK) for s in SHIFTS
        ]
        if day.weekday() == 6:
            windows.append((day.replace(hour=8 + idx), 120, StopReason.PLANNED_MAINTENANCE))
        for w_start, w_len, w_reason in windows:
            a = int((w_start - start).total_seconds() // 60)
            b = min(n_min, a + w_len)
            a = max(0, a)
            if a < b:
                state[a:b] = _PLANNED
                reason[a:b] = [w_reason.value] * (b - a)
    return state, reason


def _events(
    machine: Machine, start: datetime, state: npt.NDArray[np.int8], reason: list[str | None]
) -> list[dict[str, Any]]:
    names = {
        _RUN: MachineState.RUNNING.value,
        _DOWN: MachineState.DOWN.value,
        _PLANNED: MachineState.PLANNED_STOP.value,
    }
    out: list[dict[str, Any]] = []
    prev: tuple[int, str | None] | None = None
    for t in range(len(state)):
        cur = (int(state[t]), reason[t])
        if cur != prev:
            out.append(
                {
                    "event_id": f"{machine.machine_id}-{len(out) + 1:06d}",
                    "machine_id": machine.machine_id,
                    "ts": _iso(start + timedelta(minutes=t)),
                    "state": names[cur[0]],
                    "reason_code": cur[1],
                }
            )
            prev = cur
    return out


def _counts(
    machine: Machine, start: datetime, state: npt.NDArray[np.int8], reason: list[str | None], rng: np.random.Generator
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for a in range(0, len(state), COUNT_WINDOW_MIN):
        window = state[a : a + COUNT_WINDOW_MIN]
        running_min = int((window == _RUN).sum())
        speed = machine.speed_factor * float(np.clip(rng.normal(1.0, 0.04), 0.7, 1.08))
        expected = running_min * 60 / machine.ideal_cycle_time_s * speed
        total = int(rng.poisson(expected)) if expected > 0 else 0
        # Scrap is higher right after a tool change / changeover and on the night shift.
        recent = reason[max(0, a - 30) : a]
        scrap_rate = machine.base_scrap_rate
        if StopReason.TOOL_CHANGE.value in recent or StopReason.SETUP_CHANGEOVER.value in recent:
            scrap_rate *= 3
        ts = start + timedelta(minutes=a)
        if ts.hour >= 22 or ts.hour < 6:
            scrap_rate *= 1.3
        scrap = int(rng.binomial(total, min(scrap_rate, 1.0))) if total else 0
        out.append(
            {
                "machine_id": machine.machine_id,
                "window_start": _iso(ts),
                "window_s": COUNT_WINDOW_MIN * 60,
                "good_count": total - scrap,
                "scrap_count": scrap,
            }
        )
    return out


def _sensors(
    machine: Machine, start: datetime, state: npt.NDArray[np.int8], rng: np.random.Generator
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    rise = _TEMP_RISE_RUNNING_C[machine.machine_type]
    temp = machine.base_temp_c
    noise_t = rng.normal(0, 0.3, len(state))
    noise_l = rng.normal(0, 4.0, len(state))
    for t in range(len(state)):
        running = state[t] == _RUN
        target = machine.base_temp_c + (rise if running else -0.3 * rise)
        temp += (target - temp) * 0.08 + noise_t[t]
        load = (68.0 if running else 2.0) + (noise_l[t] if running else abs(noise_l[t]) / 4)
        out.append(
            {
                "machine_id": machine.machine_id,
                "ts": _iso(start + timedelta(minutes=t)),
                "temperature_c": round(temp, 2),
                "spindle_load_pct": round(float(np.clip(load, 0, 120)), 1),
            }
        )
    return out


def _inject_defects(data: SimulatedData, rng: np.random.Generator) -> None:
    """Corrupt a small, controlled fraction of records the way real feeds go wrong."""

    def pick(rows: Sequence[dict[str, Any]], frac: float) -> list[int]:
        k = max(1, int(len(rows) * frac))
        return sorted(rng.choice(len(rows), size=k, replace=False).tolist())

    ev = data.events
    # Duplicated messages (at-least-once delivery) and a shuffled tail (late arrival).
    for i in pick(ev, 0.01):
        ev.append(dict(ev[i]))
    tail = ev[-200:]
    ev[-200:] = [tail[j] for j in rng.permutation(len(tail))]
    for i in pick(ev, 0.002):
        ev[i] = {**ev[i], "machine_id": "CNC-99"}  # machine not in master data
    for i in pick(ev, 0.002):
        ev[i] = {**ev[i], "state": "RUNNNING"}  # typo from a hand-configured gateway

    for i in pick(data.counts, 0.002):
        data.counts[i] = {**data.counts[i], "scrap_count": -data.counts[i]["scrap_count"] - 1}
    for i in pick(data.counts, 0.001):
        data.counts[i] = {**data.counts[i], "window_start": None}

    for i in pick(data.sensors, 0.003):
        data.sensors[i] = {**data.sensors[i], "temperature_c": 999.9}  # sensor fault code
    for i in pick(data.sensors, 0.002):
        data.sensors[i] = {**data.sensors[i], "spindle_load_pct": None}


def simulate(
    start: datetime = DEFAULT_START,
    days: int = 14,
    seed: int = 42,
    machines: Sequence[Machine] = MACHINES,
    defects: bool = True,
) -> SimulatedData:
    """Generate ``days`` of synthetic data for ``machines`` starting at ``start``."""
    if days < 1:
        raise ValueError("days must be >= 1")
    rng = np.random.default_rng(seed)
    n_min = days * 1440
    data = SimulatedData()
    for idx, machine in enumerate(machines):
        state, reason = _timeline(machine, idx, start, n_min, rng)
        data.events += _events(machine, start, state, reason)
        data.counts += _counts(machine, start, state, reason, rng)
        data.sensors += _sensors(machine, start, state, rng)
    if defects:
        _inject_defects(data, rng)
    return data


EVENTS_FILE = "machine_events.jsonl"
COUNTS_FILE = "production_counts.jsonl"
SENSORS_FILE = "sensor_readings.csv"
SENSOR_COLUMNS = ("machine_id", "ts", "temperature_c", "spindle_load_pct")


def write_raw(data: SimulatedData, out_dir: Path) -> dict[str, int]:
    """Write the three raw feeds to ``out_dir``; returns row counts per file."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in ((EVENTS_FILE, data.events), (COUNTS_FILE, data.counts)):
        with (out_dir / name).open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
    with (out_dir / SENSORS_FILE).open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SENSOR_COLUMNS)
        writer.writeheader()
        writer.writerows(data.sensors)
    return {EVENTS_FILE: len(data.events), COUNTS_FILE: len(data.counts), SENSORS_FILE: len(data.sensors)}
