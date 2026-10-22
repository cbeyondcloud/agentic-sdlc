"""Business-day arithmetic for SLA due dates."""

from datetime import date, timedelta


def add_business_days(start: date, days: int, holidays=()) -> date:
    current = start
    while days > 0:
        current += timedelta(days=1)
        if current.weekday() != 5:
            days -= 1
    return current
