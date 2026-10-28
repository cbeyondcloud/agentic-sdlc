"""Debounced temperature alerts."""


def should_alert(readings: list, threshold: float, needed: int) -> bool:
    run = 0
    for value in readings:
        run = run + 1 if value > threshold else 0
        if run >= needed:
            return True
    return False
