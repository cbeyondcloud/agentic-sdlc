"""The Executive ROI dashboard (M3-01) on the executive cost seed, read as the tenant's
dashboard reader exactly as Grafana would: every panel returns rows; the month-over-month
contributions of the six factors add up to the change in cost; the what-if projection is
the target users times the per-user median of every other factor; the unattributed tile is
the untagged cost; the ROI measures say why an unmeasured value is NULL."""

from datetime import UTC, datetime
from decimal import Decimal
from statistics import median

import pytest
from grafana_macros import TimeRange, expand
from roi_seed import OCT, SEP, attributed, cost, unattributed

from agentic_cp.dashboards.catalog import EXECUTIVE_ROI

pytestmark = pytest.mark.anyio

WINDOW = TimeRange(datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 11, 30, tzinfo=UTC))


async def _values(reader, overrides=None) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for variable in EXECUTIVE_ROI.variables:
        if variable.kind == "textbox":
            values[variable.name] = [variable.default]
        else:
            rows = await reader.fetch(expand(variable.sql, WINDOW, {}))
            values[variable.name] = [str(row[0]) for row in rows]
    return {**values, **(overrides or {})}


def _panel(title: str):
    return next(p for p in EXECUTIVE_ROI.panels if p.title == title)


async def _run(reader, title: str, overrides=None):
    sql = expand(_panel(title).sql, WINDOW, await _values(reader, overrides))
    return await reader.fetch(sql)


async def test_every_panel_returns_rows(roi_reader):
    values = await _values(roi_reader)
    assert values["month"] == [OCT, SEP]  # newest first
    for panel in EXECUTIVE_ROI.panels:
        rows = await roi_reader.fetch(expand(panel.sql, WINDOW, values))
        assert rows, panel.title
        if panel.kind == "stat":
            assert len(rows) == 1 and rows[0][0] is not None, panel.title


async def test_month_over_month_contributions_add_up(roi_reader):
    rows = await _run(roi_reader, "Month-over-month change by factor")
    by_factor = {row["factor"]: row for row in rows}
    total = by_factor.pop("Cost (product of factors)")
    assert abs(total["previous"] - cost(attributed(SEP))) < Decimal("1e-9")
    assert abs(total["current"] - cost(attributed(OCT))) < Decimal("1e-9")
    assert len(by_factor) == 6
    contributions = sum(row["contribution_usd"] for row in by_factor.values())
    assert abs(contributions - (total["current"] - total["previous"])) < Decimal("1e-5")


async def test_what_if_is_target_users_times_per_user_medians(roi_reader):
    users = await _run(roi_reader, "Cost drivers per user")
    measured = [row for row in users if row["actual_usd"] is not None]
    factors = (
        "sessions_per_user",
        "turns_per_session",
        "requests_per_turn",
        "tokens_per_request",
        "cost_per_token",
    )
    [projected] = await _run(roi_reader, "What-if: projected monthly cost")
    per_month = await roi_reader.fetch(
        "select * from dash_cost_drivers_user where actual_usd is not null"
    )
    expected = Decimal(1000)
    for factor in factors:
        expected *= Decimal(str(median(float(row[factor]) for row in per_month)))
    assert float(projected[0]) == pytest.approx(float(expected), abs=0.005)  # cents
    assert {row["user_email"] for row in measured} == {"alice@a.test", "bob@a.test"}
    [ten] = await _run(roi_reader, "What-if: projected monthly cost", {"target_users": ["10"]})
    assert float(ten[0]) == pytest.approx(float(expected) / 100, abs=0.005)


async def test_unattributed_tile_is_the_untagged_cost(roi_reader):
    [row] = await _run(roi_reader, "Unattributed cost")
    assert row[0] == cost(unattributed())


async def test_roi_measures_give_a_reason_for_every_null(roi_reader):
    rows = await _run(roi_reader, "ROI measures")
    for row in rows:
        if row["value"] is None:
            assert row["reason"], row["metric"]
    named = {row["metric"]: row for row in rows}
    assert named["Human wait at gates"]["value"] == Decimal("1.5")
    assert named["DORA lead time for changes"]["value"] is None
    assert named["Gateway savings"]["value"] is None
