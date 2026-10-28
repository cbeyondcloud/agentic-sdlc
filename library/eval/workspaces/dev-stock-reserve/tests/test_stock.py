import pytest

from stock import OutOfStock, reserve


def test_reserving_reduces_stock():
    stock = {"a": 5}
    reserve(stock, {"a": 2})
    assert stock == {"a": 3}


def test_stock_never_goes_negative():
    stock = {"a": 1}
    with pytest.raises(OutOfStock):
        reserve(stock, {"a": 2})
    assert stock == {"a": 1}


def test_all_lines_or_none():
    stock = {"a": 5, "b": 0}
    with pytest.raises(OutOfStock):
        reserve(stock, {"a": 2, "b": 1})
    assert stock == {"a": 5, "b": 0}
