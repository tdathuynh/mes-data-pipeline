"""Command line entry point: ``mes-pipeline {simulate,run,report,demo}``."""

from __future__ import annotations

import argparse
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from .kpi import aggregate_kpis
from .load import connect
from .pipeline import run_pipeline
from .simulator import DEFAULT_START, simulate, write_raw

DEFAULT_RAW = Path("data/raw")
DEFAULT_DB = Path("data/mes.duckdb")


def _simulate(args: argparse.Namespace) -> None:
    data = simulate(start=args.start, days=args.days, seed=args.seed, defects=not args.clean)
    counts = write_raw(data, args.out)
    for name, n in counts.items():
        print(f"  wrote {n:>8,} rows -> {args.out / name}")


def _run(args: argparse.Namespace) -> None:
    summary = run_pipeline(args.raw, args.db)
    print(summary.run_log[["source", "rows_read", "rows_valid", "rows_rejected"]].to_string(index=False))
    print(f"duplicates removed: {summary.duplicates_removed}")
    print(f"database: {args.db}")


def _report(args: argparse.Namespace) -> None:
    con = connect(args.db, read_only=True)
    try:
        shift_kpi = con.execute("SELECT * FROM v_shift_kpi").df()
    finally:
        con.close()
    pd.options.display.float_format = "{:.1%}".format
    cols = ["availability", "performance", "quality", "oee", "scrap_rate"]
    by_machine = aggregate_kpis(shift_kpi, ["machine_id"])
    by_shift = aggregate_kpis(shift_kpi, ["shift"])
    plant = aggregate_kpis(shift_kpi, [])
    print("OEE by machine\n" + by_machine[["machine_id", *cols]].to_string(index=False))
    print("\nOEE by shift\n" + by_shift[["shift", *cols]].to_string(index=False))
    print(f"\nPlant OEE: {plant['oee'].iloc[0]:.1%}")


def _demo(args: argparse.Namespace) -> None:
    _simulate(argparse.Namespace(start=DEFAULT_START, days=args.days, seed=args.seed, clean=False, out=args.raw))
    _run(args)
    _report(args)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="mes-pipeline", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("simulate", help="generate synthetic raw shop-floor data")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--start", type=datetime.fromisoformat, default=DEFAULT_START, help="ISO timestamp")
    p.add_argument("--clean", action="store_true", help="do not inject data-quality defects")
    p.add_argument("--out", type=Path, default=DEFAULT_RAW)
    p.set_defaults(func=_simulate)

    for name, func, hlp in (
        ("run", _run, "extract, validate, transform and load into DuckDB"),
        ("report", _report, "print KPI summary from the database"),
        ("demo", _demo, "simulate + run + report in one go"),
    ):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--raw", type=Path, default=DEFAULT_RAW)
        p.add_argument("--db", type=Path, default=DEFAULT_DB)
        if name == "demo":
            p.add_argument("--days", type=int, default=14)
            p.add_argument("--seed", type=int, default=42)
        p.set_defaults(func=func)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args.func(args)


if __name__ == "__main__":  # pragma: no cover
    main()
