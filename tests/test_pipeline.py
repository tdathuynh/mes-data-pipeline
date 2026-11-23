from __future__ import annotations

from pathlib import Path

import pytest

from mes_pipeline.cli import main
from mes_pipeline.config import MACHINES
from mes_pipeline.load import connect
from mes_pipeline.pipeline import run_pipeline
from mes_pipeline.simulator import simulate, write_raw


def test_simulation_is_reproducible() -> None:
    a = simulate(days=1, seed=7, machines=MACHINES[:2])
    b = simulate(days=1, seed=7, machines=MACHINES[:2])
    assert a.events == b.events
    assert a.counts == b.counts


def test_clean_simulation_has_no_rejects(tmp_path: Path) -> None:
    write_raw(simulate(days=2, seed=1, defects=False), tmp_path / "raw")
    summary = run_pipeline(tmp_path / "raw", tmp_path / "mes.duckdb")
    assert summary.run_log["rows_rejected"].sum() == 0
    assert summary.duplicates_removed == 0


@pytest.fixture
def loaded_db(tmp_path: Path) -> Path:
    write_raw(simulate(days=3, seed=3), tmp_path / "raw")
    db = tmp_path / "mes.duckdb"
    run_pipeline(tmp_path / "raw", db)
    return db


def test_defects_are_caught_and_kept(loaded_db: Path) -> None:
    con = connect(loaded_db, read_only=True)
    by_source = dict(con.execute("SELECT source, count(*) FROM etl_rejects GROUP BY source").fetchall())
    assert set(by_source) == {"machine_events.jsonl", "production_counts.jsonl", "sensor_readings.csv"}
    errors = [r[0] for r in con.execute("SELECT error FROM etl_rejects").fetchall()]
    assert any("unknown machine_id" in e for e in errors)
    assert any(e.startswith("temperature_c") for e in errors)


def test_every_scheduled_second_is_accounted_for(loaded_db: Path) -> None:
    con = connect(loaded_db, read_only=True)
    df = con.execute("SELECT * FROM v_shift_kpi").df()
    assert len(df) == len(MACHINES) * 3 * 3  # machines x days x shifts
    # Every full shift is exactly 8 h and splits into planned stop + run + downtime.
    assert (df["scheduled_s"] == 8 * 3600).all()
    assert ((df["planned_stop_s"] + df["run_s"] + df["downtime_s"]) == df["scheduled_s"]).all()
    for col in ("availability", "performance", "quality", "oee"):
        assert df[col].between(0, 1).all(), col


def test_rerun_is_idempotent_but_run_log_grows(loaded_db: Path, tmp_path: Path) -> None:
    con = connect(loaded_db, read_only=True)
    before = con.execute("SELECT count(*) FROM fact_state_interval").fetchone()
    con.close()
    run_pipeline(tmp_path / "raw", loaded_db)
    con = connect(loaded_db, read_only=True)
    assert con.execute("SELECT count(*) FROM fact_state_interval").fetchone() == before
    assert con.execute("SELECT count(DISTINCT run_id) FROM etl_run_log").fetchone() == (2,)


def test_cli_demo(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["demo", "--days", "1", "--raw", str(tmp_path / "raw"), "--db", str(tmp_path / "x.duckdb")])
    out = capsys.readouterr().out
    assert "Plant OEE" in out
    assert "CNC-01" in out
