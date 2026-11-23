"""Pydantic models: the contract every raw record must satisfy before it is loaded."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import MACHINES_BY_ID, PLANNED_REASONS, MachineState, StopReason


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    machine_id: str

    @field_validator("machine_id")
    @classmethod
    def _known_machine(cls, v: str) -> str:
        if v not in MACHINES_BY_ID:
            raise ValueError(f"unknown machine_id {v!r} (not in master data)")
        return v


class MachineEvent(_Record):
    event_id: str = Field(min_length=1)
    ts: datetime
    state: MachineState
    reason_code: StopReason | None = None

    @model_validator(mode="after")
    def _reason_matches_state(self) -> MachineEvent:
        if self.state is MachineState.RUNNING and self.reason_code is not None:
            raise ValueError("RUNNING events must not carry a reason_code")
        if self.state is MachineState.DOWN and (self.reason_code is None or self.reason_code in PLANNED_REASONS):
            raise ValueError("DOWN events need an unplanned reason_code")
        if self.state is MachineState.PLANNED_STOP and self.reason_code not in PLANNED_REASONS:
            raise ValueError("PLANNED_STOP events need a planned reason_code")
        return self


class ProductionCount(_Record):
    window_start: datetime
    window_s: int = Field(gt=0, le=3600)
    good_count: int = Field(ge=0)
    scrap_count: int = Field(ge=0)


class SensorReading(_Record):
    ts: datetime
    temperature_c: float = Field(ge=-20, le=300)
    spindle_load_pct: float = Field(ge=0, le=150)
