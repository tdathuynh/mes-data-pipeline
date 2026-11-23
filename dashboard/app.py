"""Streamlit dashboard for the MES KPIs.

Run with:  streamlit run dashboard/app.py
(after `mes-pipeline demo` has created data/mes.duckdb)
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from mes_pipeline.config import MACHINES, PLANNED_REASONS
from mes_pipeline.kpi import aggregate_kpis
from mes_pipeline.load import connect

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "mes.duckdb"
# Fixed categorical order: a machine keeps its color whatever the filter shows.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
MACHINE_COLORS = {m.machine_id: SERIES[i] for i, m in enumerate(MACHINES)}
ACCENT = SERIES[0]
WORLD_CLASS_OEE = 0.85

st.set_page_config(page_title="MES OEE dashboard", layout="wide")


@st.cache_data
def load_tables(db: str) -> dict[str, pd.DataFrame]:
    con = connect(db, read_only=True)
    try:
        return {
            "kpi": con.execute("SELECT * FROM v_shift_kpi").df(),
            "downtime": con.execute("SELECT * FROM v_downtime_by_reason").df(),
            "rejects": con.execute(
                "SELECT source, split_part(error, ':', 1) AS field, count(*) AS records "
                "FROM etl_rejects GROUP BY ALL ORDER BY records DESC"
            ).df(),
            "runs": con.execute("SELECT * FROM etl_run_log ORDER BY started_at DESC LIMIT 3").df(),
        }
    finally:
        con.close()


@st.cache_data
def load_sensor(db: str, machine_id: str, start: str, end: str) -> pd.DataFrame:
    con = connect(db, read_only=True)
    try:
        return con.execute(
            """
            SELECT time_bucket(INTERVAL 15 MINUTE, ts) AS ts,
                   avg(temperature_c) AS temperature_c, avg(spindle_load_pct) AS load_pct
            FROM fact_sensor
            WHERE machine_id = ? AND shift_date BETWEEN ?::DATE AND ?::DATE
              AND ts >= (SELECT max(ts) FROM fact_sensor WHERE machine_id = ? AND shift_date <= ?::DATE)
                        - INTERVAL 48 HOUR
            GROUP BY 1 ORDER BY 1
            """,
            [machine_id, start, end, machine_id, end],
        ).df()
    finally:
        con.close()


def style(fig: go.Figure, height: int = 320) -> go.Figure:
    fig.update_layout(
        template="plotly_white",
        height=height,
        margin={"l": 10, "r": 10, "t": 10, "b": 10},
        legend={"orientation": "h", "y": -0.2, "title": None},
        font={"size": 13},
    )
    fig.update_xaxes(showgrid=False, title=None)
    fig.update_yaxes(gridcolor="#eceae6", title=None, zeroline=False)
    return fig


if not DB_PATH.exists():
    st.error("No database found. Run `mes-pipeline demo` first to generate data/mes.duckdb.")
    st.stop()

data = load_tables(str(DB_PATH))
kpi, downtime = data["kpi"], data["downtime"]
kpi["shift_date"] = pd.to_datetime(kpi["shift_date"]).dt.date
downtime["shift_date"] = pd.to_datetime(downtime["shift_date"]).dt.date

st.title("Shop-floor OEE dashboard")
st.caption("Synthetic data from a simulated plant (6 machines, 3 shifts). Not real production data.")

# ---- Filters (one row) -------------------------------------------------------
f1, f2, f3 = st.columns([2, 3, 1])
dmin, dmax = kpi["shift_date"].min(), kpi["shift_date"].max()
dates = f1.date_input("Date range", (dmin, dmax), min_value=dmin, max_value=dmax)
start, end = (dates[0], dates[1]) if isinstance(dates, tuple) and len(dates) == 2 else (dmin, dmax)
all_machines = [m.machine_id for m in MACHINES]
machines = f2.multiselect("Machines", all_machines, default=all_machines) or all_machines
shifts = f3.multiselect("Shifts", ["A", "B", "C"], default=["A", "B", "C"]) or ["A", "B", "C"]


def keep(df: pd.DataFrame) -> pd.DataFrame:
    mask = df["shift_date"].between(start, end) & df["machine_id"].isin(machines) & df["shift"].isin(shifts)
    return df[mask]


k = keep(kpi)
d = keep(downtime)
plant = aggregate_kpis(k, []).iloc[0]

# ---- Headline tiles ------------------------------------------------------------
tiles = st.columns(6)
tiles[0].metric("OEE", f"{plant['oee']:.1%}", f"{(plant['oee'] - WORLD_CLASS_OEE) * 100:+.1f} pp vs 85% target")
tiles[1].metric("Availability", f"{plant['availability']:.1%}")
tiles[2].metric("Performance", f"{plant['performance']:.1%}")
tiles[3].metric("Quality", f"{plant['quality']:.1%}")
tiles[4].metric("Scrap rate", f"{plant['scrap_rate']:.2%}")
tiles[5].metric("Unplanned downtime", f"{plant['downtime_s'] / 3600:,.0f} h")

# ---- OEE by machine & trend --------------------------------------------------
c1, c2 = st.columns([2, 3])
with c1:
    st.subheader("OEE by machine")
    st.caption("Dotted line: 85 % world-class benchmark")
    by_m = aggregate_kpis(k, ["machine_id"]).sort_values("oee")
    fig = px.bar(
        by_m,
        x="oee",
        y="machine_id",
        orientation="h",
        text=by_m["oee"].map("{:.0%}".format),
        custom_data=["availability", "performance", "quality"],
    )
    fig.update_traces(
        marker_color=ACCENT,
        textposition="outside",
        hovertemplate="%{y}<br>OEE %{x:.1%}<br>A %{customdata[0]:.1%} · P %{customdata[1]:.1%}"
        " · Q %{customdata[2]:.1%}<extra></extra>",
    )
    fig.add_vline(x=WORLD_CLASS_OEE, line_dash="dot", line_color="#8a8984")
    fig.update_xaxes(tickformat=".0%", range=[0, 1.05])
    st.plotly_chart(style(fig), use_container_width=True)
with c2:
    st.subheader("Daily OEE trend")
    daily = aggregate_kpis(k, ["shift_date", "machine_id"])
    fig = px.line(
        daily,
        x="shift_date",
        y="oee",
        color="machine_id",
        color_discrete_map=MACHINE_COLORS,
        category_orders={"machine_id": all_machines},
    )
    fig.update_traces(line_width=2, hovertemplate="%{x}<br>OEE %{y:.1%}")
    fig.update_layout(hovermode="x unified")
    fig.update_yaxes(tickformat=".0%")
    st.plotly_chart(style(fig), use_container_width=True)

# ---- Downtime & shift comparison ---------------------------------------------
c3, c4 = st.columns([3, 2])
with c3:
    st.subheader("Unplanned downtime by reason")
    planned = {r.value for r in PLANNED_REASONS}
    pareto = (
        d[~d["reason_code"].isin(planned)]
        .groupby("reason_code", as_index=False)
        .agg(hours=("duration_s", lambda s: s.sum() / 3600), stops=("stops", "sum"))
        .sort_values("hours")
    )
    fig = px.bar(pareto, x="hours", y="reason_code", orientation="h", text=pareto["hours"].map("{:,.0f} h".format))
    fig.update_traces(
        marker_color=ACCENT,
        textposition="outside",
        customdata=pareto[["stops"]],
        hovertemplate="%{y}<br>%{x:,.1f} h in %{customdata[0]} stops<extra></extra>",
    )
    st.plotly_chart(style(fig), use_container_width=True)
with c4:
    st.subheader("OEE by machine and shift")
    grid = aggregate_kpis(k, ["machine_id", "shift"]).pivot_table(index="machine_id", columns="shift", values="oee")
    fig = px.imshow(
        grid,
        text_auto=".0%",
        color_continuous_scale=["#f7faff", "#b5d1f3", "#5d9be6"],
        aspect="auto",
        zmin=0.5,
        zmax=0.9,
    )
    fig.update_traces(textfont_color="#0b0b0b")
    fig.update_traces(hovertemplate="%{y} · shift %{x}<br>OEE %{z:.1%}<extra></extra>")
    fig.update_coloraxes(showscale=False)
    st.plotly_chart(style(fig), use_container_width=True)

# ---- Sensor drill-down & data quality --------------------------------------------
c5, c6 = st.columns([3, 2])
with c5:
    st.subheader("Machine temperature, last 48 h of range")
    sel = st.selectbox("Machine", machines, label_visibility="collapsed")
    sensor = load_sensor(str(DB_PATH), sel, str(start), str(end))
    fig = px.line(sensor, x="ts", y="temperature_c")
    fig.update_traces(line_color=MACHINE_COLORS[sel], line_width=2, hovertemplate="%{x}<br>%{y:.1f} °C<extra></extra>")
    fig.update_yaxes(ticksuffix=" °C")
    st.plotly_chart(style(fig, 260), use_container_width=True)
with c6:
    st.subheader("Data quality: rejected records")
    st.dataframe(data["rejects"], hide_index=True, use_container_width=True)
    runs = data["runs"]
    if not runs.empty:
        last = runs.iloc[0]
        st.caption(f"Last ETL run {last['run_id']} at {last['started_at']:%Y-%m-%d %H:%M} UTC")

with st.expander("Shift-level KPI table"):
    cols = [
        "shift_date",
        "shift",
        "machine_id",
        "availability",
        "performance",
        "quality",
        "oee",
        "good_count",
        "scrap_count",
    ]
    st.dataframe(
        k[cols].sort_values(["shift_date", "shift", "machine_id"]),
        hide_index=True,
        use_container_width=True,
        column_config={c: st.column_config.NumberColumn(format="percent") for c in cols[3:7]},
    )
