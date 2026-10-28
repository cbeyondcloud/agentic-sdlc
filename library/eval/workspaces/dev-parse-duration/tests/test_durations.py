import pytest

from durations import parse_duration


def test_hours_and_minutes():
    assert parse_duration("1h30m") == 5400


def test_seconds():
    assert parse_duration("45s") == 45
    assert parse_duration("2m30s") == 150


def test_unknown_units_are_refused():
    with pytest.raises(ValueError):
        parse_duration("3d")
    with pytest.raises(ValueError):
        parse_duration("ten minutes")
