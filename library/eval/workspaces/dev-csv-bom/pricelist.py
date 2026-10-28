"""Read uploaded price lists."""

import csv
import io


def read_prices(data: bytes) -> dict:
    """{sku: price} from a CSV with 'sku' and 'price' columns."""
    rows = csv.DictReader(io.StringIO(data.decode("utf-8")))
    return {row["sku"]: row["price"] for row in rows}
