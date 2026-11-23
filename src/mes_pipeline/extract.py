"""Ingest raw feeds and validate every record against the pydantic contracts.

Invalid records are never silently dropped: they are returned as *rejects*
(source, line number, reason, raw payload) and loaded into ``etl_rejects`` so
data-quality issues stay visible downstream.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from .models import MachineEvent, ProductionCount, SensorReading
from .simulator import COUNTS_FILE, EVENTS_FILE, SENSORS_FILE

M = TypeVar("M", bound=BaseModel)


@dataclass(frozen=True)
class Reject:
    source: str
    line_no: int
    error: str
    raw: str


@dataclass
class ValidationResult(Generic[M]):
    source: str
    valid: list[M] = field(default_factory=list)
    rejects: list[Reject] = field(default_factory=list)

    @property
    def rows_read(self) -> int:
        return len(self.valid) + len(self.rejects)


def _read_jsonl(path: Path) -> Iterator[tuple[int, Any]]:
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                yield line_no, json.loads(line)
            except json.JSONDecodeError as exc:
                yield line_no, exc  # surfaced as a reject below


def _read_csv(path: Path) -> Iterator[tuple[int, Any]]:
    with path.open(encoding="utf-8", newline="") as f:
        # Line 1 is the header; empty cells mean "missing", not "empty string".
        for line_no, row in enumerate(csv.DictReader(f), start=2):
            yield line_no, {k: (v if v != "" else None) for k, v in row.items()}


def _short_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    loc = ".".join(str(p) for p in err["loc"]) or "record"
    return f"{loc}: {err['msg']}"


def validate_rows(source: str, rows: Iterator[tuple[int, Any]], model: type[M]) -> ValidationResult[M]:
    result: ValidationResult[M] = ValidationResult(source)
    for line_no, payload in rows:
        if isinstance(payload, Exception):
            result.rejects.append(Reject(source, line_no, f"malformed JSON: {payload}", ""))
            continue
        try:
            result.valid.append(model.model_validate(payload))
        except ValidationError as exc:
            result.rejects.append(Reject(source, line_no, _short_error(exc), json.dumps(payload, default=str)))
    return result


@dataclass
class RawBatch:
    events: ValidationResult[MachineEvent]
    counts: ValidationResult[ProductionCount]
    sensors: ValidationResult[SensorReading]

    @property
    def rejects(self) -> list[Reject]:
        return self.events.rejects + self.counts.rejects + self.sensors.rejects


def extract(raw_dir: Path) -> RawBatch:
    """Read and validate all three feeds from ``raw_dir``."""
    missing = [n for n in (EVENTS_FILE, COUNTS_FILE, SENSORS_FILE) if not (raw_dir / n).exists()]
    if missing:
        raise FileNotFoundError(f"missing raw files in {raw_dir}: {', '.join(missing)}")
    return RawBatch(
        events=validate_rows(EVENTS_FILE, _read_jsonl(raw_dir / EVENTS_FILE), MachineEvent),
        counts=validate_rows(COUNTS_FILE, _read_jsonl(raw_dir / COUNTS_FILE), ProductionCount),
        sensors=validate_rows(SENSORS_FILE, _read_csv(raw_dir / SENSORS_FILE), SensorReading),
    )
