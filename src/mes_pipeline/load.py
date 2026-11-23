"""Load fact tables, rejects and the run log into DuckDB, then (re)create KPI views.

The load is a full refresh inside one transaction, so re-running the pipeline is
idempotent and readers never see a half-loaded database.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import duckdb
import pandas as pd

from .config import MACHINES
from .extract import Reject
from .kpi import VIEWS
from .transform import Facts


def connect(db_path: Path | str, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path), read_only=read_only)


def _replace(con: duckdb.DuckDBPyConnection, table: str, df: pd.DataFrame) -> None:
    con.register("_staging", df)
    con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM _staging")
    con.unregister("_staging")


def load(con: duckdb.DuckDBPyConnection, facts: Facts, rejects: list[Reject], run_log: pd.DataFrame) -> dict[str, int]:
    """Write everything and return row counts per table."""
    dim_machine = pd.DataFrame(
        [
            {
                "machine_id": m.machine_id,
                "name": m.name,
                "line": m.line,
                "machine_type": m.machine_type,
                "ideal_cycle_time_s": m.ideal_cycle_time_s,
            }
            for m in MACHINES
        ]
    )
    reject_df = pd.DataFrame([asdict(r) for r in rejects], columns=["source", "line_no", "error", "raw"])
    tables = {
        "dim_machine": dim_machine,
        "fact_state_interval": facts.state_intervals,
        "fact_production": facts.production,
        "fact_sensor": facts.sensors,
        "etl_rejects": reject_df,
    }
    con.execute("BEGIN TRANSACTION")
    try:
        for name, df in tables.items():
            _replace(con, name, df)
        # The run log is append-only: it keeps the history of every pipeline run.
        con.register("_staging", run_log)
        con.execute("CREATE TABLE IF NOT EXISTS etl_run_log AS SELECT * FROM _staging WHERE false")
        con.execute("INSERT INTO etl_run_log SELECT * FROM _staging")
        con.unregister("_staging")
        for view in VIEWS:
            con.execute(view)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return {name: len(df) for name, df in tables.items()}
