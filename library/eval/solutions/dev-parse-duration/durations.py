"""Durations like '1h30m' in the scheduler config."""

import re

_UNITS = {"h": 3600, "m": 60, "s": 1}
_PART = re.compile(r"(\d+)([a-z])")


def parse_duration(text: str) -> int:
    """The duration in seconds."""
    if not re.fullmatch(r"(?:\d+[a-z])+", text):
        raise ValueError(f"not a duration: {text!r}")
    total = 0
    for amount, unit in _PART.findall(text):
        if unit not in _UNITS:
            raise ValueError(f"unknown unit {unit!r} in {text!r}")
        total += int(amount) * _UNITS[unit]
    return total
