"""Invoice arithmetic."""

from decimal import ROUND_HALF_UP, Decimal


def line_total(unit_price: str, quantity: int) -> str:
    """The line total in currency units, as a string with two decimals."""
    total = Decimal(unit_price) * quantity
    return str(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
