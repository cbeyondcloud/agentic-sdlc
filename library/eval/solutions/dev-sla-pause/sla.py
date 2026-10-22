"""SLA clock for support tickets. Events are (minute, name) in time order."""


def elapsed_minutes(events: list, now: int) -> int:
    total, running_since = 0, None
    for minute, name in events:
        if name in ("opened", "resumed") and running_since is None:
            running_since = minute
        elif name == "waiting" and running_since is not None:
            total += minute - running_since
            running_since = None
    if running_since is not None:
        total += now - running_since
    return total
