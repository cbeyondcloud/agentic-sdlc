"""The chargeback wire contract (M3-01): one CSV writer shared by the control plane and the
CLI, exact decimals, empty cells for unmeasured values, LF line endings on every OS."""

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from agentic.orgapi.cost import (
    CSV_COLUMNS,
    Chargeback,
    ChargebackRow,
    chargeback_csv,
    previous_month,
)


def _row(team: str, cost: str | None, **over) -> ChargebackRow:
    values = {
        "month": "2026-10",
        "team": team,
        "users": 1,
        "runs": 2,
        "step_executions": 5,
        "calls": 3,
        "input_tokens": 3000,
        "output_tokens": 600,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "tokens": 3600,
        "attributed_usd": Decimal(cost) if cost else None,
        "unattributed_usd": None,
        "cost_usd": Decimal(cost) if cost else None,
    }
    return ChargebackRow(**{**values, **over})


def test_csv_has_one_line_per_team_and_empty_cells_for_unmeasured():
    report = Chargeback.of(
        "2026-10",
        None,
        [_row("payments", "0.300000"), _row("search", None, unattributed_usd=None)],
    )
    text = chargeback_csv(report)
    lines = text.split("\n")
    assert "\r" not in text and text.endswith("\n")
    assert lines[0] == ",".join(CSV_COLUMNS)
    assert lines[1].startswith("2026-10,payments,1,2,5,3,3000,600,,,3600,0.300000,,0.300000")
    assert lines[2].endswith(",,,")  # no cost measured: empty, never 0
    assert len(lines) == 4  # header, two teams, final newline


def test_totals_add_the_measured_values_only():
    report = Chargeback.of(
        "2026-10",
        None,
        [
            _row(
                "payments", "0.300000", unattributed_usd=Decimal("0.05"), cost_usd=Decimal("0.35")
            ),
            _row("search", None),
        ],
    )
    assert report.totals.cost_usd == Decimal("0.35")
    assert report.totals.unattributed_usd == Decimal("0.05")
    assert report.totals.calls == 6
    empty = Chargeback.of("2026-10", "payments", [])
    assert empty.totals.cost_usd is None and empty.totals.calls == 0


def test_amounts_travel_as_exact_text():
    report = Chargeback.of("2026-10", None, [_row("payments", "0.100000")])
    dumped = report.model_dump(mode="json")
    assert dumped["rows"][0]["cost_usd"] == "0.100000"
    assert Chargeback.model_validate(dumped) == report


@pytest.mark.parametrize("month", ["2026-13", "2026-1", "26-10", "2026-10-01", ""])
def test_month_is_yyyy_mm(month):
    with pytest.raises(ValidationError):
        Chargeback.of(month, None, [])


def test_previous_month_crosses_the_year():
    assert previous_month(date(2026, 1, 15)) == "2025-12"
    assert previous_month(date(2026, 10, 1)) == "2026-09"
