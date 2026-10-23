"""Grafana's SQL macros and template variables, expanded the way its PostgreSQL datasource
expands them, so a panel's query can run on the seeded database exactly as Grafana would
send it (M2-38, Review Focus 5).

Grafana finds a macro with the pattern `$__name(args)`, where the arguments end at the first
`)`, and splits them on commas. An argument holding a parenthesis would therefore be cut
short in Grafana; `expand` refuses it, and refuses any macro this module does not know,
rather than guessing."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

MACRO = re.compile(r"\$__(\w+)\(([^)]*)\)")
FORMATTED = re.compile(r"\$\{(\w+)(?::(\w+))?\}")
PLAIN = re.compile(r"\$(?!__)(\w+)")
INTERVALS = {"1m": 60, "5m": 300, "1h": 3600, "1d": 86400}


class MacroError(ValueError):
    """A macro or variable Grafana would not expand to valid SQL."""


@dataclass(frozen=True)
class TimeRange:
    start: datetime
    end: datetime


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _instant(moment: datetime) -> str:
    return _quote(moment.isoformat())


def _macro(name: str, args: list[str], window: TimeRange) -> str:
    if name == "timeFilter" and len(args) == 1:
        start, end = _instant(window.start), _instant(window.end)
        return f"{args[0]} BETWEEN {start} AND {end}"
    if name == "timeGroupAlias" and len(args) == 2:
        seconds = INTERVALS.get(args[1].strip("'"))
        if seconds is None:
            raise MacroError(f"unknown interval {args[1]}")
        return f'floor(extract(epoch from {args[0]})/{seconds})*{seconds} AS "time"'
    if name == "timeFrom" and args == [""]:
        return _instant(window.start)
    if name == "timeTo" and args == [""]:
        return _instant(window.end)
    raise MacroError(f"unknown macro $__{name}({', '.join(args)})")


def _value(name: str, fmt: str | None, values: Mapping[str, Sequence[str]]) -> str:
    if name not in values:
        raise MacroError(f"unknown variable ${name}")
    chosen = values[name]
    if fmt not in (None, "sqlstring"):
        raise MacroError(f"unknown format ${{{name}:{fmt}}}")
    # Grafana's SQL datasources quote each value of a variable and join them with commas
    return ",".join(_quote(v) for v in chosen)


def expand(sql: str, window: TimeRange, values: Mapping[str, Sequence[str]]) -> str:
    """`sql` with every macro and variable replaced; MacroError if Grafana could not."""

    def macro(match: re.Match) -> str:
        raw = match.group(2)
        if "(" in raw:
            raise MacroError(f"nested parentheses in {match.group(0)}")
        return _macro(match.group(1), [a.strip() for a in raw.split(",")], window)

    expanded = MACRO.sub(macro, sql)
    if "$__" in expanded:
        raise MacroError(f"unexpanded macro in {sql}")
    expanded = FORMATTED.sub(lambda m: _value(m.group(1), m.group(2), values), expanded)
    return PLAIN.sub(lambda m: _value(m.group(1), None, values), expanded)
