from __future__ import annotations

import pandas as pd
import pytest

from mes_pipeline import kpi
from mes_pipeline.load import connect, load
from mes_pipeline.models import MachineEvent, ProductionCount
from mes_pipeline.transform import build_facts


class TestPureFormulas:
    def test_textbook_example(self) -> None:
        a = kpi.availability(run_s=25_200, planned_production_s=27_000)
        p = kpi.performance(ideal_cycle_time_s=45, total_count=500, run_s=25_200)
        q = kpi.quality(good_count=480, total_count=500)
        assert a == pytest.approx(0.9333, abs=1e-4)
        assert p == pytest.approx(0.8929, abs=1e-4)
        assert q == pytest.approx(0.96)
        assert kpi.oee(a, p, q) == pytest.approx(0.80)

    def test_oee_equals_good_output_over_planned_time(self) -> None:
        # Identity: A x P x Q = (good x ideal cycle time) / planned production time (when P is not capped)
        run, planned, ideal, good, total = 20_000.0, 26_000.0, 30.0, 550, 600
        o = kpi.oee(kpi.availability(run, planned), kpi.performance(ideal, total, run), kpi.quality(good, total))
        assert o == pytest.approx(good * ideal / planned)

    def test_performance_is_capped_at_one(self) -> None:
        # A counter that over-reports (or a wrong ideal cycle time) must not push OEE above 100 %.
        assert kpi.performance(ideal_cycle_time_s=60, total_count=1000, run_s=3600) == 1.0

    @pytest.mark.parametrize(
        "value",
        [
            kpi.availability(100, 0),
            kpi.performance(45, 10, 0),
            kpi.quality(0, 0),
            kpi.scrap_rate(0, 0),
            kpi.oee(0.9, None, 0.99),
        ],
    )
    def test_undefined_ratios_are_none_not_zero(self, value: float | None) -> None:
        assert value is None

    def test_scrap_rate_complements_quality(self) -> None:
        assert kpi.scrap_rate(20, 500) == pytest.approx(1 - kpi.quality(480, 500))  # type: ignore[operator]


class TestAggregation:
    def test_ratios_are_recomputed_not_averaged(self) -> None:
        shifts = pd.DataFrame(
            {
                "machine_id": ["M", "M"],
                "planned_production_s": [27_000.0, 3_000.0],  # a full shift and a short one
                "run_s": [27_000.0, 0.0],
                "downtime_s": [0.0, 3_000.0],
                "good_count": [600, 0],
                "scrap_count": [0, 0],
                "total_count": [600, 0],
                "ideal_cycle_time_s": [45.0, 45.0],
            }
        )
        agg = kpi.aggregate_kpis(shifts, ["machine_id"]).iloc[0]
        assert agg["availability"] == pytest.approx(27_000 / 30_000)  # not mean(1.0, 0.0) = 0.5
        assert agg["oee"] == pytest.approx(600 * 45 / 30_000)

    def test_empty_grouping_gives_single_plant_row(self) -> None:
        shifts = pd.DataFrame(
            {
                "planned_production_s": [100.0],
                "run_s": [50.0],
                "downtime_s": [50.0],
                "good_count": [1],
                "scrap_count": [1],
                "total_count": [2],
                "ideal_cycle_time_s": [10.0],
            }
        )
        out = kpi.aggregate_kpis(shifts, [])
        assert len(out) == 1
        assert out["availability"].iloc[0] == pytest.approx(0.5)
        assert out["performance"].iloc[0] == pytest.approx(0.4)
        assert out["quality"].iloc[0] == pytest.approx(0.5)


class TestSqlViews:
    @pytest.fixture
    def shift_kpi(self, shift_a_events: list[MachineEvent], shift_a_counts: list[ProductionCount]) -> pd.Series:
        facts = build_facts(shift_a_events, shift_a_counts, [])
        con = connect(":memory:")
        load(con, facts, [], pd.DataFrame({"run_id": ["test"]}))
        df = con.execute("SELECT * FROM v_shift_kpi").df()
        assert len(df) == 1
        return df.iloc[0]

    def test_time_buckets(self, shift_kpi: pd.Series) -> None:
        assert shift_kpi["scheduled_s"] == 28_800
        assert shift_kpi["planned_stop_s"] == 1_800
        assert shift_kpi["planned_production_s"] == 27_000
        assert shift_kpi["run_s"] == 25_200
        assert shift_kpi["downtime_s"] == 1_800

    def test_sql_matches_python(self, shift_kpi: pd.Series) -> None:
        a = kpi.availability(25_200, 27_000)
        p = kpi.performance(45, 500, 25_200)
        q = kpi.quality(480, 500)
        assert shift_kpi["availability"] == pytest.approx(a)
        assert shift_kpi["performance"] == pytest.approx(p)
        assert shift_kpi["quality"] == pytest.approx(q)
        assert shift_kpi["oee"] == pytest.approx(0.80)
        assert shift_kpi["scrap_rate"] == pytest.approx(0.04)

    def test_downtime_view(self, shift_a_events: list[MachineEvent], shift_a_counts: list[ProductionCount]) -> None:
        con = connect(":memory:")
        load(con, build_facts(shift_a_events, shift_a_counts, []), [], pd.DataFrame({"run_id": ["t"]}))
        rows = con.execute(
            "SELECT reason_code, stops, duration_s FROM v_downtime_by_reason ORDER BY reason_code"
        ).fetchall()
        assert rows == [("BREAK", 1, 1800.0), ("TOOL_CHANGE", 1, 1800.0)]
