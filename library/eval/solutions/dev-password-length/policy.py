"""Password policy."""


def long_enough(password: str, minimum: int = 8) -> bool:
    return len(password) >= minimum
