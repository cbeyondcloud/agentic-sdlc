"""Stock reservations for store pickup."""


class OutOfStock(Exception):
    pass


def reserve(stock: dict, lines: dict) -> None:
    """Reserve `lines` ({sku: quantity}) from `stock` ({sku: on hand}), in place."""
    short = sorted(sku for sku, quantity in lines.items() if stock.get(sku, 0) < quantity)
    if short:
        raise OutOfStock(", ".join(short))
    for sku, quantity in lines.items():
        stock[sku] -= quantity
