"""Debounced temperature alerts."""


def should_alert(readings: list, threshold: float, needed: int) -> bool:
    above = 0
    for value in readings:
        if value > threshold:
            above += 1
    return above >= needed
