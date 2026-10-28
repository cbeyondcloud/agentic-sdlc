"""Durations like '1h30m' in the scheduler config."""

import re

_PART = re.compile(r"(\d+)([hm])")


def parse_duration(text: str) -> int:
    """The duration in seconds."""
    total = 0
    for amount, unit in _PART.findall(text):
        total += int(amount) * (3600 if unit == "h" else 60)
    return total
