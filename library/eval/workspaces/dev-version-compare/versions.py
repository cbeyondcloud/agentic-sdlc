"""Compare release versions for the update checker."""


def is_newer(candidate: str, current: str) -> bool:
    return candidate > current
