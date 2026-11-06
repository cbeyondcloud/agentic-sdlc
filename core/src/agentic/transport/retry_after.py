"""The one `Retry-After` reader for agentic's HTTP clients (DF-986).

RFC 9110 section 10.2.3 allows two forms: delta-seconds (`120`) and an HTTP date
(`Wed, 21 Oct 2026 07:28:00 GMT`). Jira, GitHub and the control plane are read with the
same rules: a date becomes whole seconds from now (0 for one already past), any wait is
capped (a day unless the caller asks for less), and a value that is neither - a negative
or non-finite number, a date too large for a datetime - is None, never an exception.
"""

import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

#: The longest wait a Retry-After is believed for: a day (review I1 of M2-50).
MAX_RETRY_AFTER = 86400.0


def retry_after_seconds(
    value: str | None, *, longest: float = MAX_RETRY_AFTER, now: datetime | None = None
) -> float | None:
    """The wait `value` (a Retry-After header) asks for, in seconds, at most `longest`;
    None when it is absent or unreadable."""
    header = (value or "").strip()
    if not header:
        return None
    try:
        seconds = float(header)
    except ValueError:
        try:
            when = parsedate_to_datetime(header)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        when = when if when.tzinfo else when.replace(tzinfo=UTC)
        try:
            delta = (when - (now or datetime.now(UTC))).total_seconds()
        except OverflowError:
            return None
        seconds = float(max(0, math.ceil(delta)))
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, longest)
