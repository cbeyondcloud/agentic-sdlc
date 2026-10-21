"""SLA clock for support tickets. Events are (minute, name) in time order."""


def elapsed_minutes(events: list, now: int) -> int:
    opened = next(minute for minute, name in events if name == "opened")
    return now - opened
