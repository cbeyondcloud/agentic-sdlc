from datetime import date

from calendar_days import add_business_days


def test_within_a_week():
    assert add_business_days(date(2026, 3, 2), 3) == date(2026, 3, 5)  # Monday -> Thursday


def test_weekends_are_skipped():
    assert add_business_days(date(2026, 3, 6), 1) == date(2026, 3, 9)  # Friday -> Monday


def test_holidays_are_skipped():
    assert add_business_days(date(2026, 3, 2), 1, holidays=[date(2026, 3, 3)]) == date(2026, 3, 4)
