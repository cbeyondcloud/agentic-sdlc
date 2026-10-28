"""Compare release versions for the update checker."""


def _key(version: str):
    core, _, pre = version.partition("-")
    numbers = tuple(int(part) for part in core.split("."))
    return numbers, pre == "", pre


def is_newer(candidate: str, current: str) -> bool:
    return _key(candidate) > _key(current)
