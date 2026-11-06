"""One Retry-After reader for every HTTP client in agentic (DF-986): delta-seconds or an
HTTP date (RFC 9110 section 10.2.3), never negative, at most a day, and None - never an
exception - for anything else."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from agentic.transport.retry_after import MAX_RETRY_AFTER, retry_after_seconds

NOW = datetime(2026, 11, 6, 10, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("header", "seconds"),
    [("7", 7.0), (" 12 ", 12.0), ("0", 0.0), ("1.5", 1.5), ("86400", 86400.0)],
)
def test_delta_seconds_are_read(header, seconds):
    assert retry_after_seconds(header, now=NOW) == seconds


def test_an_http_date_is_whole_seconds_from_now():
    header = format_datetime(NOW + timedelta(seconds=90, milliseconds=300), usegmt=True)
    assert retry_after_seconds(header, now=NOW) == 90.0  # the header has whole seconds
    assert retry_after_seconds("Fri, 06 Nov 2026 10:02:00 GMT", now=NOW) == 120.0


def test_an_http_date_without_a_zone_is_read_as_utc():
    assert retry_after_seconds("Fri, 06 Nov 2026 10:01:00 -0000", now=NOW) == 60.0


@pytest.mark.parametrize(
    "header", ["Fri, 06 Nov 2026 09:00:00 GMT", "Thu, 01 Jan 1970 00:00:00 GMT"]
)
def test_a_date_already_past_means_now(header):
    assert retry_after_seconds(header, now=NOW) == 0.0


@pytest.mark.parametrize(
    "header",
    ["999999999", "1e300", "Wed, 04 Nov 9999 10:00:00 GMT", "Fri, 06 Nov 2027 10:00:00 GMT"],
)
def test_a_wait_over_a_day_is_capped(header):
    assert retry_after_seconds(header, now=NOW) == MAX_RETRY_AFTER == 86400.0


def test_the_cap_can_be_lower():
    assert retry_after_seconds("120", longest=10.0, now=NOW) == 10.0


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "soon",
        "-5",
        "nan",
        "inf",
        "-inf",
        "Wed, 04 Nov 99999999999999999999 10:00:00 GMT",  # OverflowError in the date parser
        "Wed, 99 Nov 2026 10:00:00 GMT",
    ],
)
def test_anything_else_is_none_not_an_exception(header):
    assert retry_after_seconds(header, now=NOW) is None


def test_now_defaults_to_the_clock():
    later = datetime.now(UTC) + timedelta(seconds=100)
    assert 95 <= retry_after_seconds(format_datetime(later, usegmt=True)) <= 101
