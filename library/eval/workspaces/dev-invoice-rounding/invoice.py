"""Invoice arithmetic."""


def line_total(unit_price: str, quantity: int) -> str:
    """The line total in currency units, as a string with two decimals."""
    return f"{round(float(unit_price) * quantity, 2):.2f}"
