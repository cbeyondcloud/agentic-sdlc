from pricelist import read_prices


def test_plain_utf8():
    assert read_prices(b"sku,price\nA1,9.99\n") == {"A1": "9.99"}


def test_excel_bom_and_crlf():
    data = "\ufeffsku,price\r\nA1,9.99\r\nÉ2,1.50\r\n".encode("utf-8")
    assert read_prices(data) == {"A1": "9.99", "É2": "1.50"}
