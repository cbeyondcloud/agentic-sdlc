"""Amounts are whole cents (int), never floats."""


def total(lines: list[int]) -> int:
    """The invoice total: the sum of its line amounts."""
    return sum(lines)


def format_cents(cents: int) -> str:
    """12345 -> '123.45'."""
    sign = "-" if cents < 0 else ""
    whole, part = divmod(abs(cents), 100)
    return f"{sign}{whole}.{part:02d}"
