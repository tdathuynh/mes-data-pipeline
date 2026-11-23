"""MES KPI definitions.

The same formulas exist twice on purpose:

* as small pure Python functions (easy to read, unit-tested), and
* as SQL views in DuckDB (what the dashboard queries).

Tests assert that both agree.

OEE = Availability x Performance x Quality, where

* Availability = run time / planned production time
* Performance  = (ideal cycle time x total count) / run time   (capped at 100 %)
* Quality      = good count / total count

Planned production time = scheduled time - planned stops (breaks, planned maintenance).
Ratios are never averaged across shifts or machines: aggregates are recomputed
from summed times and counts (see :func:`aggregate_kpis`).
"""

from __future__ import annotations

import pandas as pd


def _ratio(num: float, den: float) -> float | None:
    return None if den <= 0 else num / den


def availability(run_s: float, planned_production_s: float) -> float | None:
    return _ratio(run_s, planned_production_s)


def performance(ideal_cycle_time_s: float, total_count: int, run_s: float) -> float | None:
    p = _ratio(ideal_cycle_time_s * total_count, run_s)
    return None if p is None else min(p, 1.0)


def quality(good_count: int, total_count: int) -> float | None:
    return _ratio(good_count, total_count)


def oee(a: float | None, p: float | None, q: float | None) -> float | None:
    if a is None or p is None or q is None:
        return None
    return a * p * q


def scrap_rate(scrap_count: int, total_count: int) -> float | None:
    return _ratio(scrap_count, total_count)


SHIFT_KPI_VIEW = """
CREATE OR REPLACE VIEW v_shift_kpi AS
WITH t AS (
    SELECT machine_id, shift_date, shift,
           sum(duration_s)                                         AS scheduled_s,
           coalesce(sum(duration_s) FILTER (WHERE state = 'PLANNED_STOP'), 0) AS planned_stop_s,
           coalesce(sum(duration_s) FILTER (WHERE state = 'RUNNING'), 0)      AS run_s,
           coalesce(sum(duration_s) FILTER (WHERE state = 'DOWN'), 0)         AS downtime_s
    FROM fact_state_interval
    GROUP BY ALL
), c AS (
    SELECT machine_id, shift_date, shift,
           sum(good_count) AS good_count, sum(scrap_count) AS scrap_count
    FROM fact_production
    GROUP BY ALL
), base AS (
    SELECT t.machine_id, m.line, m.machine_type, t.shift_date, t.shift,
           t.scheduled_s, t.planned_stop_s,
           t.scheduled_s - t.planned_stop_s AS planned_production_s,
           t.run_s, t.downtime_s,
           coalesce(c.good_count, 0)  AS good_count,
           coalesce(c.scrap_count, 0) AS scrap_count,
           coalesce(c.good_count, 0) + coalesce(c.scrap_count, 0) AS total_count,
           m.ideal_cycle_time_s
    FROM t
    JOIN dim_machine m USING (machine_id)
    LEFT JOIN c USING (machine_id, shift_date, shift)
), ratios AS (
    SELECT *,
           run_s / nullif(planned_production_s, 0)                     AS availability,
           least(ideal_cycle_time_s * total_count / nullif(run_s, 0), 1.0) AS performance,
           good_count / nullif(total_count, 0)                          AS quality,
           scrap_count / nullif(total_count, 0)                         AS scrap_rate
    FROM base
)
SELECT *, availability * performance * quality AS oee
FROM ratios
"""

DOWNTIME_VIEW = """
CREATE OR REPLACE VIEW v_downtime_by_reason AS
SELECT machine_id, shift_date, shift, state, reason_code,
       count(DISTINCT source_event_id) AS stops,
       sum(duration_s)                 AS duration_s
FROM fact_state_interval
WHERE state <> 'RUNNING'
GROUP BY ALL
"""

VIEWS = (SHIFT_KPI_VIEW, DOWNTIME_VIEW)


def aggregate_kpis(shift_kpi: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    """Roll ``v_shift_kpi`` rows up to any grain (e.g. machine, day, line).

    Times and counts are summed first, then the ratios are recomputed - averaging
    per-shift OEE values would weight a short half-empty shift like a full one.
    The performance cap is applied after aggregation.
    """
    sums = ["planned_production_s", "run_s", "downtime_s", "good_count", "scrap_count", "total_count"]
    df = shift_kpi.assign(ideal_output_s=shift_kpi["ideal_cycle_time_s"] * shift_kpi["total_count"])
    g = (
        df.groupby(by, as_index=False)[[*sums, "ideal_output_s"]].sum()
        if by
        else (df[[*sums, "ideal_output_s"]].sum().to_frame().T)
    )
    pp = g["planned_production_s"].where(g["planned_production_s"] > 0)
    run = g["run_s"].where(g["run_s"] > 0)
    tot = g["total_count"].where(g["total_count"] > 0)
    g["availability"] = g["run_s"] / pp
    g["performance"] = (g["ideal_output_s"] / run).clip(upper=1.0)
    g["quality"] = g["good_count"] / tot
    g["scrap_rate"] = g["scrap_count"] / tot
    g["oee"] = g["availability"] * g["performance"] * g["quality"]
    out: pd.DataFrame = g.drop(columns="ideal_output_s")
    return out
