"""Stock reservations for store pickup."""


class OutOfStock(Exception):
    pass


def reserve(stock: dict, lines: dict) -> None:
    """Reserve `lines` ({sku: quantity}) from `stock` ({sku: on hand}), in place."""
    for sku, quantity in lines.items():
        stock[sku] = stock.get(sku, 0) - quantity
