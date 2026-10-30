"""The monthly chargeback (M3-01): its wire models and the one CSV writer the control plane
(`GET /v1/cost/chargeback?format=csv`) and the CLI (`agentic report chargeback`) share, so
the two files are byte-identical.

One row per team for one month, from the control plane's `dash_chargeback` view: a
registered run's calls belong to the run's team and to the month the run opened; a call the
control plane cannot attribute to a run belongs to its own month and authenticated team and
is counted in `unattributed_usd`. Amounts are exact decimals (text in JSON). A value nobody
measured is None (an empty CSV cell), never 0."""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, StringConstraints

#: A calendar month, `YYYY-MM`.
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
Month = Annotated[str, StringConstraints(pattern=MONTH_PATTERN)]

#: The CSV header, in order (also the row model's field order).
CSV_COLUMNS: tuple[str, ...] = (
    "month",
    "team",
    "users",
    "runs",
    "step_executions",
    "calls",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "tokens",
    "attributed_usd",
    "unattributed_usd",
    "cost_usd",
)
_SUMMED = CSV_COLUMNS[2:]


class ChargebackRow(BaseModel):
    """One team's month: people and runs counted, calls and tokens summed, cost split into
    the attributed part, the unattributed part and their total."""

    month: Month
    team: str
    users: int
    runs: int
    step_executions: int | None
    calls: int
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    tokens: int | None
    attributed_usd: Decimal | None
    unattributed_usd: Decimal | None
    cost_usd: Decimal | None


class ChargebackTotals(BaseModel):
    """The rows' sums (users are summed per team: a person in two teams counts twice)."""

    users: int
    runs: int
    step_executions: int | None
    calls: int
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    tokens: int | None
    attributed_usd: Decimal | None
    unattributed_usd: Decimal | None
    cost_usd: Decimal | None


class Chargeback(BaseModel):
    """The chargeback of `month` (for `team`, or every team when None)."""

    month: Month
    team: str | None
    rows: list[ChargebackRow]
    totals: ChargebackTotals

    @classmethod
    def of(cls, month: str, team: str | None, rows: list[ChargebackRow]) -> Chargeback:
        totals = {}
        for column in _SUMMED:
            measured = [getattr(row, column) for row in rows if getattr(row, column) is not None]
            counted = column in ("users", "runs", "calls")
            totals[column] = sum(measured) if measured or counted else None
        return cls(month=month, team=team, rows=rows, totals=ChargebackTotals(**totals))


def _cell(value: object) -> str:
    return "" if value is None else str(value)


def chargeback_csv(report: Chargeback) -> str:
    """The rows as CSV: the `CSV_COLUMNS` header, one line per team, LF line endings."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for row in report.rows:
        writer.writerow([_cell(getattr(row, column)) for column in CSV_COLUMNS])
    return out.getvalue()


def previous_month(today: date) -> str:
    """The calendar month before `today`'s, `YYYY-MM` (the default chargeback month)."""
    year, month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    return f"{year:04d}-{month:02d}"
